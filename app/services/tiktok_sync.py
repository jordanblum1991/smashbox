"""TikTok sync orchestration — per-stream, incremental, watermarked.

For each stream (orders / settlements / payouts / analytics) it: reads the
watermark, pulls everything since, feeds it through that stream's importer seam,
then advances the watermark and records the run status. Reused by a manual "Sync
now" button and the weekday scheduler.

All four streams are LIVE against the approved Shop API (built + validated on
prod 2026-06-15; see app/services/tiktok_fetchers.py). `_fetch_stream` dispatches
each to its fetcher; the `NotImplementedError` it can raise is only the fallback
for an unrecognised stream name, NOT a not-yet-built state. When the shop isn't
connected, `run_sync` records each stream as "pending" (not failed) so the
framework + status panel stay usable.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.import_batch import _utc_now_naive
from app.models.tiktok_sync_state import TikTokSyncState

STREAMS = ("orders", "settlements", "payouts", "analytics")
DEFAULT_LOOKBACK_DAYS = 7  # first sync (no watermark) pulls this much history
STALE_HOURS = 36  # past the daily cadence (+grace) → the auto-sync has stalled


def sync_health(db: Session) -> dict | None:
    """Return an attention signal when the auto-sync needs a look, else None.
    Only when the shop is connected. Read-only — does NOT create sync-state rows
    (so it's safe to call from a render path / middleware).

      {"severity": "error", "reason": "error", "streams": [...]}  — a stream failed
      {"severity": "warn",  "reason": "stale", "hours": N}        — hasn't run in >36h
    """
    from app.services.tiktok_api import get_credential

    cred = get_credential(db)
    if cred is None or not cred.shop_cipher:
        return None
    states = db.query(TikTokSyncState).all()
    errored = [s.stream for s in states if s.last_status == "error"]
    if errored:
        return {"severity": "error", "reason": "error", "streams": errored}
    if not settings.tiktok_auto_sync_enabled:
        return None
    runs = [s.last_run_at for s in states if s.last_run_at]
    if runs:
        hours = (_utc_now_naive() - max(runs)).total_seconds() / 3600
        if hours > STALE_HOURS:
            return {"severity": "warn", "reason": "stale", "hours": int(hours)}
    return None


def get_state(db: Session, stream: str) -> TikTokSyncState:
    state = db.execute(
        select(TikTokSyncState).where(TikTokSyncState.stream == stream)
    ).scalar_one_or_none()
    if state is None:
        state = TikTokSyncState(stream=stream)
        db.add(state)
        db.flush()
    return state


def all_states(db: Session) -> list[TikTokSyncState]:
    """The three stream states in canonical order (creating any missing)."""
    return [get_state(db, s) for s in STREAMS]


def _fetch_stream(db: Session, stream: str, cred, since) -> int:
    """Pull `stream` records since `since` from the TikTok API, map them to the
    stream's DataFrame shape, and feed the matching importer seam. Returns rows
    imported.

    All four streams (orders / settlements / payouts / analytics) are live — see
    app.services.tiktok_fetchers. The trailing NotImplementedError fires only for
    an unrecognised stream name."""
    if stream == "orders":
        from app.services.tiktok_fetchers import fetch_orders, refresh_order_statuses
        # Incremental pull of NEW orders (create-time watermark), then refresh the
        # status of recently-created orders whose state has since advanced — the
        # watermark never re-pulls them, so without this their status freezes.
        n = fetch_orders(db, cred, since)
        n += refresh_order_statuses(db, cred)
        return n
    if stream == "settlements":
        from app.services.tiktok_fetchers import fetch_settlements
        return fetch_settlements(db, cred, since)
    if stream == "payouts":
        from app.services.tiktok_fetchers import fetch_payouts
        return fetch_payouts(db, cred, since)
    if stream == "analytics":
        from app.services.tiktok_fetchers import fetch_analytics
        return fetch_analytics(db, cred, since)
    raise NotImplementedError(
        f"TikTok {stream} fetcher is wired once the app is approved and the shop is connected."
    )


def run_sync(db: Session, *, streams: tuple[str, ...] = STREAMS, source: str = "manual") -> dict[str, str]:
    """Run a sync across `streams`. Never raises — every stream's outcome is
    recorded on its TikTokSyncState. Returns {stream: status}."""
    from app.services.tiktok_api import ensure_fresh_token, get_credential

    cred = get_credential(db)
    connected = cred is not None and bool(cred.shop_cipher)

    summary: dict[str, str] = {}
    now = _utc_now_naive()
    for stream in streams:
        state = get_state(db, stream)
        state.last_run_at = now
        if not connected:
            state.last_status = "pending"
            state.last_message = "TikTok not connected — connect the app on this page first."
            summary[stream] = "pending"
            continue
        try:
            fresh = ensure_fresh_token(db) or cred
            since = state.synced_through or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))
            n = _fetch_stream(db, stream, fresh, since)
            state.synced_through = now
            state.rows_last_run = n
            state.last_status = "ok" if n else "empty"
            state.last_message = None
            summary[stream] = state.last_status
        except NotImplementedError as exc:
            state.last_status = "pending"
            state.last_message = str(exc)
            summary[stream] = "pending"
        except Exception as exc:  # noqa: BLE001
            state.last_status = "error"
            state.last_message = str(exc)
            summary[stream] = "error"
    db.commit()
    return summary
