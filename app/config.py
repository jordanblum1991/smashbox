from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # File-system locations. In production these point at the Fly persistent
    # volume mount (`/data`, `/data/uploads`, `/data/exports`); locally they
    # default to the project directory. Override via env vars on deploy.
    data_dir: Path = REPO_ROOT / "data"
    upload_dir: Path = REPO_ROOT / "uploads"
    export_dir: Path = REPO_ROOT / "exports"

    # SQLite file lives inside data_dir. Set DATABASE_URL explicitly to point
    # at Postgres later; otherwise we derive it from data_dir at startup.
    database_url: str | None = None

    default_brand: str = "smashbox"

    # ---- SAP inventory feed ----------------------------------------------
    # Live on-hand snapshot endpoint (replaces the manual inventory CSV upload).
    # Returns JSON rows of {Itemcode, WhsCode, OnHand, InventoryDate}; we keep
    # the SB (sellable) warehouse for demand planning. Override in prod via
    # `fly secrets set SAP_INVENTORY_URL=...` if the endpoint/token rotates.
    sap_inventory_url: str = "https://api.fhiheat.com/PoKde7rmxb.php"
    sap_inventory_warehouse: str = "SB"  # sellable warehouse code in the feed

    # Whether the in-process APScheduler runs at all. OFF by default so the test
    # suite and local dev never spawn a background scheduler; production turns it
    # on with `fly secrets set SCHEDULER_ENABLED=true`. WHEN/whether the inventory
    # sync actually fires is a separate, user-editable setting persisted on the
    # Shop row (inventory_sync_enabled/hour/minute/days) — this flag only gates
    # the scheduler machinery itself. The values below seed a fresh Shop.
    scheduler_enabled: bool = False
    inventory_sync_default_hour: int = 7
    inventory_sync_default_minute: int = 30
    inventory_sync_default_days: str = "mon,tue,wed,thu,fri"

    # ---- Auth (per-user sessions) -----------------------------------------
    # Phase 1: real users instead of the v1 shared HTTP Basic credential.
    #
    # session_secret signs the session cookie (Starlette SessionMiddleware via
    # itsdangerous). Empty string = auth disabled — convenient for local dev
    # so reloads don't sign you out. Production MUST set this to a long random
    # string via `fly secrets set SESSION_SECRET=...`.
    session_secret: str = ""

    # First-time bootstrap. When no User rows exist and these are populated,
    # the startup hook creates an admin user with these credentials. Set
    # before the first deploy, then unset (or leave alone — they're ignored
    # once a user exists).
    initial_admin_email: str = ""
    initial_admin_password: str = ""
    initial_admin_name: str = "Admin"

    # ---- Auth (legacy, kept for backward-compat with old deploys) ---------
    # Phase 1's session middleware is the new gate. These remain so a fresh
    # deploy that hasn't yet set SESSION_SECRET stays protected by Basic Auth
    # rather than wide-open. Empty values keep the legacy path inert.
    basic_auth_username: str = "smashbox"
    basic_auth_password: str = ""

    # ---- Business-rule caps -----------------------------------------------
    # Cap on Outlandish-funded portion of a seller-funded discount, as a
    # fraction of the order's eligible base. Smashbox absorbs anything over.
    # See app/rules/seller_funded_split.py.
    outlandish_cap_pct: Decimal = Decimal("0.10")

    # Policy: total seller-funded discount should NEVER exceed this fraction of
    # the eligible base. Anything over is imported (Smashbox still absorbs it
    # so the exact-sum invariant holds) but flagged as a policy violation.
    seller_funded_policy_cap_pct: Decimal = Decimal("0.30")

    # ---- Demand planning defaults ----------------------------------------
    # Tunable on the planner page via query-string overrides (?safety, ?cover);
    # a Phase D settings UI will make these per-shop persistent. Per the
    # 2026-05 product brief: 10% safety, 14-day default lead time, 45-day
    # cover, 180-day overstock threshold.
    demand_safety_stock_pct: Decimal = Decimal("0.10")
    demand_lead_time_default_days: int = 14
    demand_cover_days: int = 45
    demand_overstocked_days: int = 180

    # ---- Velocity spike dampening ----------------------------------------
    # `daily_60d` is a flat 60-day mean — one viral spike inflates it for the
    # whole window and drives overbuying. The robust rate clips each day at
    # the max of (cap_mult × median of non-zero days) and (mean_mult × raw mean),
    # so a real outlier gets capped but normal high-volume days don't. The
    # units gate avoids churning dead SKUs without dropping live-but-lumpy ones.
    velocity_spike_cap_mult: Decimal = Decimal("3.0")
    velocity_raw_mean_mult: Decimal = Decimal("5.0")
    velocity_min_units_for_dampening: int = 5

    # ---- Variance-based safety stock -------------------------------------
    # Replaces the flat-percentage method. safety_stock = z × σ_daily × √L,
    # where σ_daily comes from the RAW (uncapped) 60-day daily series so the
    # buffer reflects real volatility — capping σ would under-buffer the
    # exact spikes safety stock is meant to absorb.
    # Service level → z-score mapping is in SERVICE_LEVEL_Z_TABLE below.
    demand_service_level_default: Decimal = Decimal("0.95")

    # ---- Slow-mover Poisson safety stock ---------------------------------
    # Gaussian z·σ·√L assumes continuous, symmetric demand. For SKUs averaging
    # <1 unit/day, real demand is Poisson (discrete, skewed); Gaussian
    # under-buffers because it pretends the SKU can sell fractional units.
    # When effective daily velocity is below this threshold we switch to
    # Poisson safety stock (inverse-CDF at the service level). The threshold
    # is evaluated on the post-cold-start velocity, so a new low-volume SKU
    # gets the uplift first, then the Poisson buffer if it's still slow.
    demand_slow_mover_threshold: Decimal = Decimal("1.0")

    # ---- Trend-adjusted reorder point ------------------------------------
    # When 14-day velocity is materially above the 60-day baseline (ratio
    # above the threshold), blend the two for the ROP base velocity so the
    # SKU trips reorder sooner. ASYMMETRIC: we ONLY blend up on acceleration.
    # Deceleration is surfaced as a UI signal but doesn't shrink ROP —
    # stocking out on a recovery is worse than tying up capital on a dip.
    # `weight_recent` is the share given to the 14-day rate (0.5 = 50/50).
    demand_trend_acceleration_threshold: Decimal = Decimal("1.2")
    demand_trend_weight_recent: Decimal = Decimal("0.5")

    # ---- Cold-start (new SKUs) -------------------------------------------
    # A SKU sold for the first time fewer than N days ago has a 60-day mean
    # polluted by pre-existence zero days. We re-mean over the days the SKU
    # has actually existed (`daily_observed = units / days_observed`) and
    # apply an uplift to widen the safety net while the buyer accumulates
    # signal. Cold-start SKUs do NOT trip the trend-adjustment branch
    # (insufficient 14d signal) — their entire velocity is already an estimate.
    demand_cold_start_threshold_days: int = 30
    demand_cold_start_uplift: Decimal = Decimal("1.5")


settings = Settings()


# Z-score lookup for the normal distribution at common service levels.
# Defined here (not as a Setting) because the math is fixed: a 95% one-sided
# service level always maps to ~1.65 standard deviations above the mean.
# Add more tiers only if business genuinely wants finer-grained tiering;
# rough levels exist deliberately so buyers think in 90 / 95 / 97.5 tiers.
SERVICE_LEVEL_Z_TABLE: dict[Decimal, Decimal] = {
    Decimal("0.90"): Decimal("1.28"),
    Decimal("0.95"): Decimal("1.65"),
    Decimal("0.975"): Decimal("1.96"),
}


def z_for_service_level(sl: Decimal) -> Decimal:
    """Return the z-score for a given service level. Decimal equality is
    value-based, so 0.95 == 0.950 == 0.9500 all match the table entry.
    Raises KeyError for unsupported levels — pad the table rather than
    guessing an interpolated z."""
    for level, z in SERVICE_LEVEL_Z_TABLE.items():
        if sl == level:
            return z
    supported = sorted(SERVICE_LEVEL_Z_TABLE.keys())
    raise KeyError(
        f"No z-score defined for service level {sl}; "
        f"supported levels: {[str(s) for s in supported]}"
    )

# Resolve the default DB URL once the data_dir is finalized.
if settings.database_url is None:
    settings.database_url = f"sqlite:///{settings.data_dir / 'smashbox.db'}"

# Ensure directories exist at every startup (including on a fresh Fly volume).
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.export_dir.mkdir(parents=True, exist_ok=True)
