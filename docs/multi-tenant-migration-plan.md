# Smashbox Dashboard → Multi-User, Multi-Brand, Externally Accessible

**Purpose:** Concise implementation plan to take the internal P&L dashboard to a Postgres-backed, multi-brand, externally accessible app where each user sees only their data.

## Current state
- FastAPI app on a single Fly.io VM (LAX); **SQLite** on a 1 GB encrypted volume.
- Auth already in place: per-user email + bcrypt, signed-cookie sessions; roles (`admin`/`member`) + a `super_admin` flag.
- Multi-tenancy **schema groundwork done**: every tenant-scoped table carries a `shop_id` FK to a `shops` table (one shop today). Queries are **not yet filtered by shop**. Data model already carries `brand` on orders/SKUs.
- DB connection is env-driven (`DATABASE_URL`); Alembic dir is reserved but **not initialized**.
- **Data ingestion is manual CSV upload today** (orders, settlements, payouts, ad spend exported from TikTok Seller Center).

## Target state
- **Self-hosted on our own server, Postgres-backed.** Multiple brands isolated at the data layer (one tenant per brand). **Internet-reachable HTTPS access for internal staff** (usable from outside the office), each user scoped to the brands they're assigned.
- **Scoped down for now: all users are internal staff.** External brand-partner access is a deliberate future phase, not in this build — the schema/roles leave room for it.

## Workstreams

**1. Database → Postgres**
- Stand up **Postgres on our own server** (dedicated instance or container) — TLS, automated backups, point-in-time recovery, restricted to the app host.
- Initialize **Alembic**; generate a baseline migration from the ORM; switch deploy/CI to `alembic upgrade head` (replaces auto-create-on-boot).
- Migrate existing SQLite data; validate row counts **and financial totals** against current prod.

**2. Tenant isolation (core app work)**
- **Decided: each brand is its own tenant** — one `shops` row per brand; `shop_id` is the hard isolation boundary. The existing `brand` column on orders/SKUs stays as a within-tenant label, not the security key.
- Scope **every report query and importer** by `shop_id` — enforced at a single choke point (a session-bound query filter) so no route can leak across tenants.
- Super-admin UI to create brands/shops, invite users, and assign each user to a shop + role.
- **User↔shop mapping is many-to-many:** staff are assigned to one or more brands (needs an in-app brand switcher). (Single-shop external partners deferred to a later phase.)

**3. Auth & access control**
- **Two roles for now:** *super-admin* (all brands + management) and *staff* (assigned brands, full reports). Deny-by-default authorization on every route (tenant + role checked server-side). A *brand-partner* (own brand, read-only) role is a later add — leave the role model open for it.
- **SSO with our corporate IdP** (Google Workspace or Microsoft 365, via OIDC) so there are no separate dashboard passwords and offboarding in the IdP revokes access. **MFA for admins.**
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

## Decisions needed
1. **SSO provider:** Google Workspace or Microsoft 365? (Picks the staff SSO integration; MFA required for admins.)

*Decided:*
- *Each brand is its own tenant (one shop per brand, isolated by `shop_id`).*
- *All users are internal staff for now; external brand-partner access is a deliberate later phase.*
- *Self-hosted on our own server, internet-reachable for staff (see workstreams 1, 4, 5).*
- *No data-residency or formal compliance/audit requirements.*
