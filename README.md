# Smashbox

Internal full-stack web app for managing the Smashbox TikTok Shop P&L and operating reports.

Upload TikTok Shop export files, get accurate monthly P&L, SKU-level profitability, sample tracking, and reconciliation summaries.

## Quick start

```bash
py -m venv .venv
.\.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
alembic upgrade head

npm install                       # one-time: Tailwind build toolchain (package.json)
npm run css                       # compile app/static/css/tailwind.css — REQUIRED before first run

uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

> **Tailwind is compiled, not CDN.** `app/static/css/tailwind.css` is a **gitignored build artifact**, so a fresh clone has no stylesheet until you run `npm run css` — skip it and every page renders unstyled (the file 404s). While doing UI work, run `npm run css:watch` in a second terminal so edits recompile automatically. (Production doesn't need this step — see [Production deployment](#production-deployment).)

## Run tests

```bash
pytest
pytest tests/test_seller_funded_split.py -v   # one file
pytest -k split                                # by keyword
```

## What lives where

See `CLAUDE.md` for the architecture map.

## Stack

FastAPI · Jinja2 + HTMX · Tailwind (compiled via `npm run css`) · SQLAlchemy + Alembic · SQLite (Postgres-ready) · pandas · openpyxl / xlsxwriter

---

## Production deployment

The app runs on **Fly.io** at https://smashbox.fly.dev/ (LAX region).

| | |
|---|---|
| **URL** | https://smashbox.fly.dev/ |
| **Auth** | Per-user sessions (Phase 1). Bootstrap admin via Fly secrets — see "Auth & user management" below. |
| **Region** | LAX (Los Angeles) |
| **Machine** | 1× shared-cpu-1x · 512MB RAM · auto-sleep when idle, auto-wake on request |
| **Volume** | `smashbox_data` · 1GB encrypted at `/data` — SQLite DB + uploads + exports all live here |
| **Image** | Built from `Dockerfile` (`python:3.12-slim`) |

### Pushing code changes

```bash
git push                       # GitHub (source of truth)
fly deploy --app smashbox      # ~30s redeploy of the Fly machine
```

Everything in the repo is shipped except dev artifacts (see `.dockerignore`). The `fly.toml` in the repo root is the deploy config.

**Tailwind CSS rebuilds automatically on deploy — no manual step.** The `Dockerfile` has a `node:24-slim` build stage that runs `npm ci && npm run css` and copies the compiled `app/static/css/tailwind.css` into the image. Because it's rebuilt from the current templates on every `fly deploy`, production can't ship stale CSS, and you never run `npm run css` by hand for a deploy (that's a local-dev-only step — see [Quick start](#quick-start)). If a deploy ever fails inside that stage (`npm ci` registry reach, or a Tailwind config error), the build aborts before a release is cut and the previous image keeps serving — no outage.

### Auth & user management (Phase 1)

The app uses per-user logins backed by bcrypt-hashed passwords and signed session cookies. Three Fly secrets drive it:

```bash
# Required in production — long random string signing session cookies
fly secrets set SESSION_SECRET="$(openssl rand -base64 32)" --app smashbox

# Used ONCE on first boot to create the seed admin user. Once a User row exists, these are ignored.
fly secrets set INITIAL_ADMIN_EMAIL="you@yourcompany.com" --app smashbox
fly secrets set INITIAL_ADMIN_PASSWORD="<strong-password>" --app smashbox
fly secrets set INITIAL_ADMIN_NAME="Jordan Blum" --app smashbox
```

Empty `SESSION_SECRET` disables auth entirely — that's the local-dev default (so you don't have to log in on every reload). **Never empty in production**; if it's empty, the legacy `BasicAuthMiddleware` kicks in as a safety net (requires `BASIC_AUTH_PASSWORD` to be set), otherwise the app is wide open.

**Adding more users** (until the user-management UI ships in Phase 1b):

```bash
fly ssh console --app smashbox -C "sh -c 'cd /app && python -c \"
from app.db import SessionLocal
from app.auth import hash_password
from app.models.user import User, UserRole
with SessionLocal() as db:
    db.add(User(email=\\\"colleague@yourcompany.com\\\", name=\\\"Their Name\\\", password_hash=hash_password(\\\"InitialPassword123\\\"), role=UserRole.MEMBER))
    db.commit()
\"'"
```

**Resetting a forgotten password** uses the same pattern but with `User.password_hash = hash_password(...)` on an existing row.

### Inspecting the running app

```bash
fly logs --app smashbox          # live tail; Ctrl-C to exit
fly status --app smashbox        # machine + volume state
fly ssh console --app smashbox   # shell into the running container
```

### Migrating data between local and production

The `/data` volume holds the SQLite DB and all uploaded files. To copy your local state up:

```powershell
# 1. Upload DB to a staging path so the running app's SQLite handle isn't disturbed
Get-Content data\smashbox.db -AsByteStream -Raw | fly ssh console --app smashbox -C "sh -c 'cat > /data/smashbox.db.new'"

# 2. Upload every file in uploads/ (skips .gitkeep)
tar -cf - -C uploads --exclude=.gitkeep . | fly ssh console --app smashbox -C "sh -c 'tar -xf - -C /data/uploads'"

# 3. Atomic swap + restart so SQLite reopens the new file cleanly
fly ssh console --app smashbox -C "sh -c 'mv /data/smashbox.db.new /data/smashbox.db'"
fly apps restart smashbox
```

To pull production data back down for inspection, reverse with `fly ssh sftp get /data/smashbox.db ./smashbox-prod.db`.

### Backup

Fly automatically snapshots volumes daily (5 retained). Restore via:

```bash
fly volumes snapshots list smashbox_data
fly volumes snapshots restore <snapshot-id>
```

### Scaling later

Current setup handles a small finance team comfortably. When you need more:

- **More users → real per-user logins.** Swap `app/auth.py` (currently HTTP Basic) for a sessions-based middleware + a `User` table. The route-protection contract stays the same.
- **More concurrent users (50+) → Postgres.** Use Fly's managed Postgres (`fly postgres create`), set `DATABASE_URL` via `fly secrets set`. SQLAlchemy abstracts the swap. **First initialize Alembic** — see the comment in `app/main.py` about migrating off `Base.metadata.create_all`.
- **More headroom → bigger VM.** Edit `[[vm]]` in `fly.toml` (`shared-cpu-2x`, `1gb` memory, etc.), then `fly deploy`.

