# Multi-User, Multi-Brand, Self-Hosted Dashboard

**Purpose:** Concise implementation plan to take the internal P&L dashboard to a Postgres-backed, multi-brand, self-hosted app where each staff user sees only the brands they're assigned.

## Where to start
**Build the foundation first; start the long-lead-time API access request in parallel.**
- **Engineering — do first (P0):** stand up Postgres → initialize Alembic → migrate and validate the SQLite data. It's the foundation everything else writes to, and Alembic must be in place *before* any schema change (multi-tenancy and the API integration both add tables). Lowest risk, no external dependencies.
- **Admin / IT — in parallel, start now:** (a) provision the server + VPN; (b) **submit the TikTok developer-app + Shop API access request** — approval carries weeks of external lead time, so start the clock now even though the integration is built later.
- **Then:** multi-tenant scoping (P1) → TikTok API ingestion once access is granted and the DB foundation is in place.
- **Do *not* start by building the API connections:** the API only replaces the ingestion mechanism (rows land in the same shape), so it unblocks nothing and would be built against SQLite then re-validated after the Postgres move.

## Current state
- FastAPI app on a single Fly.io VM (LAX); **SQLite** on a 1 GB encrypted volume.
- Auth already in place: per-user email + bcrypt, signed-cookie sessions; roles (`admin`/`member`) + a `super_admin` flag.
- Multi-tenancy **schema groundwork done**: every tenant-scoped table carries a `shop_id` FK to a `shops` table (one shop today). Queries are **not yet filtered by shop**. Data model already carries `brand` on orders/SKUs.
- DB connection is env-driven (`DATABASE_URL`); Alembic dir is reserved but **not initialized**.
- **Data ingestion is manual CSV upload today** (orders, settlements, payouts, ad spend exported from TikTok Seller Center).

## Target state
- **Self-hosted on our own server, Postgres-backed.** Multiple brands isolated at the data layer (one tenant per brand). **Internet-reachable HTTPS access for internal staff** (usable from outside the office), each user scoped to the brands they're assigned.
- **Scoped down for now: all users are internal staff.** External brand-partner access is a deliberate future phase, not in this build — the schema/roles leave room for it.

## Architecture — single-server, containerized

Simplest design that fits the load (a handful of internal users, a few GB of data): one Linux server, everything in Docker Compose, reachable only over the corporate VPN. No Kubernetes / microservices / Redis / read replicas — not warranted yet.

```
  Staff (remote)
       │  HTTPS, over corporate VPN only
       ▼
┌───────────────────────────────────────────────┐
│  One Linux server (our data center / VM)        │
│  Docker Compose:                                │
│                                                 │
│   Caddy  ──►  FastAPI app  ──►  Postgres 16     │
│  (reverse     (Uvicorn/        (localhost-only, │
│   proxy,       Gunicorn,        persistent      │
│   auto-TLS)    N workers)       volume)         │
│                    │                            │
│              Scheduler/worker ──► TikTok API    │
│              (future API sync)    (outbound)    │
└───────────────────────────────────────────────┘
       │
   Nightly encrypted backup ──► off-box (object storage / 2nd host)

  Auth: Google Workspace OIDC (redirect login)
```

- **One Linux VM**, orchestrated by **Docker Compose** — reproducible, portable, no cluster to operate.
- **Caddy** reverse proxy: HTTPS termination + automatic Let's Encrypt certs.
- **FastAPI** (existing app) under Gunicorn+Uvicorn workers, containerized.
- **Postgres 16** container with a persistent volume, **bound to localhost** — never internet-exposed.
- **Scheduler/worker** for the future TikTok API sync: an APScheduler process or system cron running a `sync` command — no Celery/Redis at this scale.
- **Network:** firewall permits only the VPN subnet on 443; outbound allowed to Google (auth) and TikTok (future sync).
- **CI/CD:** GitHub Actions builds the image, runs tests + `alembic upgrade head` against a throwaway Postgres, deploys to the server, runs migrations + health check.
- **Graduation path (only when load demands it):** move Postgres to a dedicated/managed instance, then run 2–3 app replicas behind the same proxy. Everything's already a container, so this is config, not a rewrite.

## Workstreams

**1. Database → Postgres**
- Stand up **Postgres on our own server** (dedicated instance or container) — TLS, automated backups, point-in-time recovery, restricted to the app host.
- Initialize **Alembic**; generate a baseline migration from the ORM; switch deploy/CI to `alembic upgrade head` (replaces auto-create-on-boot).
- Migrate existing SQLite data; validate row counts **and financial totals** against current prod.

*Database details:*
- **Isolation model:** a *single shared Postgres database*; brands separated by `shop_id` row scoping — **not** a database or schema per brand. Simplest to operate; revisit only if a brand ever needs hard physical separation.
- **Size:** small dataset (months of TikTok orders/settlements — well under a few GB), so modest server specs; no replication/HA or sharding needed initially.
- **Config:** Postgres 16, UTF-8, **store timestamps in UTC** (per-brand timezone is a display concern only).
- **Backups:** nightly logical dump + WAL archiving for point-in-time recovery; encrypted, retained off-box, with a periodic restore test.
- **Access:** DB bound to localhost/private network only; app uses a least-privilege role; credentials in the secret store.
- **Pooling:** SQLAlchemy's built-in connection pool is sufficient at this scale (add PgBouncer only if connection counts grow).

**2. Tenant isolation (core app work)**
- **Decided: each brand is its own tenant** — one `shops` row per brand; `shop_id` is the hard isolation boundary. The existing `brand` column on orders/SKUs stays as a within-tenant label, not the security key.
- Scope **every report query and importer** by `shop_id` — enforced at a single choke point (a session-bound query filter) so no route can leak across tenants.
- Super-admin UI to create brands/shops, invite users, and assign each user to a shop + role.
- **User↔shop mapping is many-to-many:** staff are assigned to one or more brands (needs an in-app brand switcher). (Single-shop external partners deferred to a later phase.)

**3. Auth & access control**
- **Two roles for now:** *super-admin* (all brands + management) and *staff* (assigned brands, full reports). Deny-by-default authorization on every route (tenant + role checked server-side). A *brand-partner* (own brand, read-only) role is a later add — leave the role model open for it.
- **SSO with Google Workspace (OIDC)** so there are no separate dashboard passwords and offboarding in Google revokes access. **MFA for admins** (enforced via Google Workspace 2-Step Verification).
- Harden sessions (Secure/HttpOnly/SameSite cookies, rotation, idle timeout); keep email/password as a fallback only.

**4. External hosting & security (self-hosted)**
- **Access model — recommended:** since all users are internal staff, put the app behind our **VPN (or an IP allowlist)** rather than the fully public internet. Staff still reach it remotely from outside the office, but no anonymous traffic ever hits it — a much smaller attack surface and simpler to operate. Lift to public later if/when external partners come online.
- Containerize the app (Docker) and run it behind a **reverse proxy** (nginx/Caddy) on our server; internal domain with **TLS via Let's Encrypt** (auto-renew).
- Lock it down for public exposure: firewall (only 443 open), static IP/DNS, secrets in env/secret store (not in code), OS + dependency patching cadence.
- Rate limiting / WAF in front; **audit logging** of logins and data exports.

**5. Operations (now our responsibility, since self-hosted)**
- Backup + **tested restore drill**; monitoring/alerting + error tracking; OS/security patching; uptime ownership.
- A **staging** environment; CI that runs migrations and tests; documented deploy + rollback.
- Capacity: size the server for concurrent external users; plan headroom as brands are added.

## Suggested sequencing
- **P0 — Foundation:** Postgres + Alembic + data migration. No user-facing change.
- **P1 — Multi-tenancy:** query/importer scoping + super-admin shop & user management.
- **P2 — External readiness:** auth hardening (SSO/MFA, lockout), custom domain, audit logs.
- **P3 — Scale & ops:** scale-out, monitoring, staging/CI maturity.

## Related roadmap item — TikTok API ingestion (separate effort)
- Planned move from **manual CSV uploads → a scheduled TikTok API sync** (orders, settlements, payouts, ad spend) for fresher, automated data.
- Touches the **importers layer only**: the API pull lands rows in the same shape, so reports, COGS resolution, the discount split, and reconciliation are unaffected. CSV upload stays as a manual fallback.
- **IT-relevant implications:** a scheduled job/worker (cron or background service), secure storage of TikTok API credentials/tokens (incl. refresh + rotation), and outbound network access from the server to TikTok's API. Loosely coupled to this migration — overlaps only at the data layer.

## Decisions (all resolved)
- Each brand is its own tenant (one shop per brand, isolated by `shop_id`).
- All users are internal staff for now; external brand-partner access is a deliberate later phase.
- Self-hosted on our own server, internet-reachable for staff — recommended behind VPN/IP-allowlist (see workstreams 1, 4, 5).
- **SSO via Google Workspace (OIDC); MFA for admins.**
- No data-residency or formal compliance/audit requirements.
