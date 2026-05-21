"""Tests for the SKU alias service.

Covers four concerns:

  1. `load_alias_map` flattens chains and survives cycles.
  2. `canonicalize` is a pure lookup with sensible None handling.
  3. `upsert_alias` is idempotent — re-applying doesn't duplicate rows.
  4. `suggest_aliases` finds same-stem pairs and temporal handoffs while
     excluding already-aliased ones, noise (below `min_units`), and self.
  5. Integration: with aliases registered, `compute_velocity` collapses
     a re-coded SKU's pre- and post-rename history into one signal.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.models.sku_alias import SkuAlias
from app.services.demand.velocity import compute_velocity
from app.models.bundle import Bundle, BundleComponent
from app.services.sku_alias import (
    _format_suggestions_verbose,
    canonicalize,
    load_alias_map,
    suggest_aliases,
    upsert_alias,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="seed.csv",
        stored_path="/tmp/seed.csv",
    )
    db.add(b)
    db.flush()
    return b


def _order(db, batch_id: int, sku: str, *,
           placed_at: datetime, qty: int = 1,
           status: str = "Shipped",
           order_type: OrderType = OrderType.PAID) -> None:
    """Insert one Order + one OrderLine."""
    order_id = f"{sku}-{int(placed_at.timestamp())}-{qty}"
    o = Order(
        import_batch_id=batch_id,
        tiktok_order_id=order_id,
        placed_at=placed_at,
        order_type=order_type,
        status=status,
        brand="smashbox",
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku, quantity=qty,
                     unit_cogs_snapshot=Decimal("10.00")))


def _orders_daily(db, batch_id: int, sku: str, *,
                  start: datetime, days: int, qty_per_day: int = 1,
                  order_type: OrderType = OrderType.PAID,
                  status: str = "Shipped") -> None:
    for i in range(days):
        _order(db, batch_id, sku,
               placed_at=start + timedelta(days=i),
               qty=qty_per_day, status=status, order_type=order_type)
    db.flush()


# ---- load_alias_map -------------------------------------------------------

def test_load_alias_map_flattens_chain():
    """A -> B and B -> C should resolve to A -> C, B -> C."""
    with SessionLocal() as db:
        db.add(SkuAlias(alias_sku="A", canonical_sku="B"))
        db.add(SkuAlias(alias_sku="B", canonical_sku="C"))
        db.commit()

        m = load_alias_map(db)
        assert m == {"A": "C", "B": "C"}


def test_load_alias_map_handles_cycle_without_infinite_loop():
    """A -> B, B -> A. Without cycle protection this would loop forever.
    The terminal output is well-defined (a particular node in the cycle),
    but more importantly: it returns."""
    with SessionLocal() as db:
        db.add(SkuAlias(alias_sku="A", canonical_sku="B"))
        db.add(SkuAlias(alias_sku="B", canonical_sku="A"))
        db.commit()

        m = load_alias_map(db)
        # Both keys present; values are A or B (terminal of the chain walk).
        assert set(m.keys()) == {"A", "B"}
        for v in m.values():
            assert v in ("A", "B")


def test_load_alias_map_empty_when_no_rows():
    with SessionLocal() as db:
        assert load_alias_map(db) == {}


def test_canonicalize_passthrough_when_not_aliased():
    assert canonicalize("UNALIASED", {"X": "Y"}) == "UNALIASED"
    assert canonicalize("X", {"X": "Y"}) == "Y"
    assert canonicalize(None, {"X": "Y"}) is None


# ---- upsert_alias ---------------------------------------------------------

def test_upsert_alias_creates_then_updates_existing():
    with SessionLocal() as db:
        upsert_alias(db, alias_sku="C09D01", canonical_sku="SBX-C09D01", notes="first")
        upsert_alias(db, alias_sku="C09D01", canonical_sku="SBX-C09D01-V2", notes="updated")
        db.commit()

        rows = db.execute(
            __import__("sqlalchemy").select(SkuAlias)
        ).scalars().all()
        # Only one row per alias_sku.
        assert len(rows) == 1
        assert rows[0].canonical_sku == "SBX-C09D01-V2"
        assert rows[0].notes == "updated"


def test_upsert_alias_rejects_self_alias():
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            upsert_alias(db, alias_sku="A", canonical_sku="A")


# ---- suggest_aliases ------------------------------------------------------

def test_suggest_aliases_finds_same_stem_pair():
    """C09D01 and SBX-C09D01 should be suggested as alias->canonical."""
    with SessionLocal() as db:
        b = _batch(db)
        # Both codes have enough volume to clear the noise floor.
        _orders_daily(db, b.id, "C09D01",
                      start=datetime(2026, 1, 1), days=14, qty_per_day=2)
        _orders_daily(db, b.id, "SBX-C09D01",
                      start=datetime(2026, 2, 1), days=14, qty_per_day=2)
        db.commit()

        suggestions = suggest_aliases(db, min_units=5)
        same_stem = [s for s in suggestions if s.reason in ("same_stem", "both")]
        assert len(same_stem) == 1
        s = same_stem[0]
        assert s.alias_sku == "C09D01"
        assert s.canonical_sku == "SBX-C09D01"
        assert s.confidence == "high"


def test_suggest_aliases_skips_already_aliased_pairs():
    """If a pair is already in sku_aliases, don't re-suggest."""
    with SessionLocal() as db:
        b = _batch(db)
        _orders_daily(db, b.id, "C09D01",
                      start=datetime(2026, 1, 1), days=14, qty_per_day=2)
        _orders_daily(db, b.id, "SBX-C09D01",
                      start=datetime(2026, 2, 1), days=14, qty_per_day=2)
        upsert_alias(db, alias_sku="C09D01", canonical_sku="SBX-C09D01")
        db.commit()

        suggestions = suggest_aliases(db, min_units=5)
        # Already approved — should not appear in the report.
        for s in suggestions:
            assert (s.alias_sku, s.canonical_sku) != ("C09D01", "SBX-C09D01")


def test_suggest_aliases_respects_min_units_floor():
    """SKUs with very few sales shouldn't flag — too noisy."""
    with SessionLocal() as db:
        b = _batch(db)
        _orders_daily(db, b.id, "C09D01",
                      start=datetime(2026, 1, 1), days=2, qty_per_day=1)
        _orders_daily(db, b.id, "SBX-C09D01",
                      start=datetime(2026, 2, 1), days=2, qty_per_day=1)
        db.commit()

        # min_units=5; each side has only 2 → filtered out.
        suggestions = suggest_aliases(db, min_units=5)
        assert suggestions == []


def test_suggest_aliases_flags_catalog_same_sku_code():
    """Two Sku rows share `Sku.sku` (the human-readable code) but have
    different `tiktok_sku_id` values → almost certainly a TikTok re-listing.
    Demand for each sits under different TikTok IDs at the order layer."""
    today = datetime.now()
    with SessionLocal() as db:
        b = _batch(db)
        # Two catalog rows, same human SKU "SBX-C6R801", different TikTok IDs.
        db.add(Sku(
            sku="SBX-C6R801", tiktok_sku_id="OLD-TT-ID",
            name="Travel Primer", brand="smashbox", unit_cogs=Decimal("8.00"),
        ))
        db.add(Sku(
            sku="SBX-C6R801", tiktok_sku_id="NEW-TT-ID",
            name="Travel Primer", brand="smashbox", unit_cogs=Decimal("8.00"),
        ))
        # Some sales under each TikTok ID so the candidate clears noise floor.
        _orders_daily(db, b.id, "OLD-TT-ID",
                      start=today - timedelta(days=60), days=14, qty_per_day=1)
        _orders_daily(db, b.id, "NEW-TT-ID",
                      start=today - timedelta(days=20), days=14, qty_per_day=1)
        db.commit()

        suggestions = suggest_aliases(db, min_units=5)
        catalog_hits = [s for s in suggestions if "catalog_same_sku_code" in s.reason]
        assert len(catalog_hits) == 1
        # Canonical = the listing with the more recent last sale (NEW-TT-ID).
        assert catalog_hits[0].canonical_sku == "NEW-TT-ID"
        assert catalog_hits[0].alias_sku == "OLD-TT-ID"


def test_suggest_aliases_flags_temporal_handoff_without_stem_match():
    """A's sales stop while B's begin → flag as temporal_handoff. Reflect
    that the user might have re-coded a product to an unrelated-looking
    name like 'PROD-V2' from 'C09D01'."""
    today = datetime.now()
    with SessionLocal() as db:
        b = _batch(db)
        # OLD: 14 days of sales ending ~45 days ago.
        old_start = today - timedelta(days=59)
        _orders_daily(db, b.id, "OLDCODE",
                      start=old_start, days=14, qty_per_day=2)
        # NEW: starts ~3 days after OLD's last sale, ~28 days ago.
        new_start = today - timedelta(days=42)
        _orders_daily(db, b.id, "RENAMED-PROD",
                      start=new_start, days=20, qty_per_day=2)
        db.commit()

        suggestions = suggest_aliases(
            db, min_units=5, max_handoff_gap_days=21, quiet_window_days=30,
        )
        handoff = [s for s in suggestions
                   if s.alias_sku == "OLDCODE" and s.canonical_sku == "RENAMED-PROD"]
        assert len(handoff) == 1
        assert handoff[0].reason in ("temporal_handoff", "both")


# ---- Integration: velocity collapses aliased history ----------------------

def test_compute_velocity_combines_aliased_demand():
    """The whole point: register an alias, and the planner sees ONE
    combined signal instead of two half-signals."""
    today = datetime.now()
    # 60-day window: today-60d .. today (midnight aligned).
    with SessionLocal() as db:
        b = _batch(db)
        # 30 days of demand under the legacy code, then 30 under the new.
        legacy_start = today - timedelta(days=58)
        new_start = today - timedelta(days=28)
        _orders_daily(db, b.id, "LEGACY",
                      start=legacy_start, days=30, qty_per_day=1)
        _orders_daily(db, b.id, "SBX-LEGACY",
                      start=new_start, days=28, qty_per_day=1)
        db.commit()

        # Without aliasing: each SKU shows ~30 units of separate history.
        unaliased = compute_velocity(db, as_of=today, alias_map={})
        legacy_units = unaliased["LEGACY"].units_60d if "LEGACY" in unaliased else 0
        new_units = unaliased["SBX-LEGACY"].units_60d if "SBX-LEGACY" in unaliased else 0
        assert legacy_units > 0
        assert new_units > 0

        # With aliasing: one combined signal under the canonical, no separate
        # legacy entry.
        upsert_alias(db, alias_sku="LEGACY", canonical_sku="SBX-LEGACY")
        db.commit()
        aliased = compute_velocity(db, as_of=today)
        assert "LEGACY" not in aliased
        assert "SBX-LEGACY" in aliased
        combined = aliased["SBX-LEGACY"].units_60d
        assert combined == legacy_units + new_units


def test_compute_velocity_alias_explicit_empty_disables_collapse():
    """Passing `alias_map={}` short-circuits the DB lookup — useful for
    tests and one-off analyses that want raw per-code behavior."""
    today = datetime.now()
    with SessionLocal() as db:
        b = _batch(db)
        _orders_daily(db, b.id, "LEGACY",
                      start=today - timedelta(days=30), days=14, qty_per_day=1)
        _orders_daily(db, b.id, "SBX-LEGACY",
                      start=today - timedelta(days=15), days=14, qty_per_day=1)
        upsert_alias(db, alias_sku="LEGACY", canonical_sku="SBX-LEGACY")
        db.commit()

        # Default (db loads aliases) → collapsed.
        with_aliases = compute_velocity(db, as_of=today)
        assert "LEGACY" not in with_aliases
        # Explicit empty → uncollapsed.
        without_aliases = compute_velocity(db, as_of=today, alias_map={})
        assert "LEGACY" in without_aliases
        assert "SBX-LEGACY" in without_aliases


# ---- Inventory rollup -----------------------------------------------------

def test_latest_on_hand_collapses_aliases_to_canonical():
    """Two snapshots under different aliased codes: the most-recent wins
    and it's keyed under canonical."""
    from app.reports.demand_planning import _latest_on_hand_per_sku

    with SessionLocal() as db:
        b = _batch(db)
        # Legacy code snapshotted Mar 1, new code Apr 1 (newer).
        db.add(InventorySnapshot(
            import_batch_id=b.id, sku="LEGACY", on_hand=100,
            captured_at=datetime(2026, 3, 1),
        ))
        db.add(InventorySnapshot(
            import_batch_id=b.id, sku="SBX-LEGACY", on_hand=80,
            captured_at=datetime(2026, 4, 1),
        ))
        upsert_alias(db, alias_sku="LEGACY", canonical_sku="SBX-LEGACY")
        db.commit()

        # With alias map: newer snapshot wins, keyed under canonical.
        m, _ = _latest_on_hand_per_sku(db, alias_map=load_alias_map(db))
        assert m == {"SBX-LEGACY": 80}

        # Without: two entries, one per code.
        m_raw, _ = _latest_on_hand_per_sku(db)
        assert set(m_raw.keys()) == {"LEGACY", "SBX-LEGACY"}


# ---- Issue 1 fix: verbose formatter resolves Bundle catalog --------------

def test_verbose_formatter_labels_bundle_codes():
    """A code that's in the Bundle table but not Sku must NOT be labelled
    "(not in catalog)" — bundles are real catalog entries; the verbose
    formatter was only checking Sku."""
    from app.services.sku_alias import AliasSuggestion
    from datetime import date

    with SessionLocal() as db:
        b = _batch(db)
        # Bundle entry only — no Sku row for this tiktok_sku_id.
        bundle = Bundle(
            bundle_sku="SBX-FAKE-BUNDLE",
            tiktok_sku_id="9999999999999999",
            name="A Fake Test Bundle",
            brand="smashbox",
        )
        db.add(bundle)
        db.commit()

        # Make a synthetic suggestion whose canonical is the bundle.
        suggestion = AliasSuggestion(
            alias_sku="LEGACY-XYZ",
            canonical_sku="9999999999999999",
            reason="temporal_handoff",
            confidence="medium",
            alias_last_sale=date(2026, 1, 1),
            canonical_first_sale=date(2026, 2, 1),
            alias_total_units=10,
            canonical_total_units=20,
            days_gap=5,
        )
        out = _format_suggestions_verbose(db, [suggestion])
        # The bundle must be labelled with [BUNDLE] prefix, NOT "(not in catalog)".
        assert "[BUNDLE]" in out
        assert "SBX-FAKE-BUNDLE" in out
        assert "A Fake Test Bundle" in out
        # The non-existent LEGACY-XYZ should still show as "(not in catalog)".
        assert "(not in catalog)" in out  # for LEGACY-XYZ side


# ---- Issue 2 fix: skip aliases already mapped to ANY canonical -----------

def test_suggest_skips_already_aliased_regardless_of_canonical():
    """Pre-fix bug: if A → B was registered and the heuristic computed
    A → C (different canonical), suggest still proposed it. Applying the
    new pair would silently overwrite the existing alias on upsert.

    Post-fix: an alias_sku already in the map is skipped entirely,
    regardless of what canonical the suggest is computing."""
    today = datetime.now()
    with SessionLocal() as db:
        b = _batch(db)
        # Three SKU codes with sales: OLDCODE went dormant, NEW1 and NEW2
        # both came alive recently. Without registration, temporal_handoff
        # might propose OLDCODE → NEW1 and OLDCODE → NEW2.
        _orders_daily(db, b.id, "OLDCODE",
                      start=today - timedelta(days=59), days=14, qty_per_day=2)
        _orders_daily(db, b.id, "NEW1",
                      start=today - timedelta(days=42), days=20, qty_per_day=2)
        _orders_daily(db, b.id, "NEW2",
                      start=today - timedelta(days=40), days=20, qty_per_day=2)

        # Register OLDCODE → NEW1 first.
        upsert_alias(db, alias_sku="OLDCODE", canonical_sku="NEW1")
        db.commit()

        # Suggest should NOT propose OLDCODE → NEW2.
        suggestions = suggest_aliases(
            db, min_units=5, max_handoff_gap_days=21, quiet_window_days=30,
        )
        oldcode_proposals = [s for s in suggestions if s.alias_sku == "OLDCODE"]
        assert oldcode_proposals == [], (
            f"OLDCODE is already aliased to NEW1; suggest should not propose "
            f"it again. Got: {[(s.alias_sku, s.canonical_sku) for s in oldcode_proposals]}"
        )
