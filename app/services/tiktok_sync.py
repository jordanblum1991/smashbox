"""TikTok sync orchestration — per-stream, incremental, watermarked.

For each stream (orders / settlements / payouts) it: reads the watermark, pulls
everything since, feeds it through that stream's `import_dataframes` seam, then
advances the watermark and records the run status. Reused by a manual "Sync now"
button and (once connected) the weekday scheduler.

The actual API fetch (`_fetch_stream`) is the one piece that needs the live,
approved TikTok connection — it's a single seam, built + verified against real
responses then. Until then a run records each stream as "pending" rather than
failing, so the framework + status panel are usable now.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.import_batch import _utc_now_naive
from app.models.tiktok_sync_state import TikTokSyncState

STREAMS = ("orders", "settlements", "payouts", "analytics")
DEFAULT_LOOKBACK_DAYS = 7  # first sync (no watermark) pulls this much history


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

    Orders are live (see app.services.tiktok_fetchers). Settlements and payouts
    still raise NotImplementedError so a run records them as 'pending' until
    their fetchers are built — keeping the orchestration + status panel usable."""
    if stream == "orders":
        from app.services.tiktok_fetchers import fetch_orders
        return fetch_orders(db, cred, since)
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
