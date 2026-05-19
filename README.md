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
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Run tests

```bash
pytest
pytest tests/test_seller_funded_split.py -v   # one file
pytest -k split                                # by keyword
```

## What lives where

See `CLAUDE.md` for the architecture map.

## Stack

FastAPI · Jinja2 + HTMX · Tailwind (CDN) · SQLAlchemy + Alembic · SQLite (Postgres-ready) · pandas · openpyxl / xlsxwriter

---

## Production deployment

The app runs on **Fly.io** at https://smashbox.fly.dev/ (LAX region).

| | |
|---|---|
| **URL** | https://smashbox.fly.dev/ |
| **Auth** | HTTP Basic — username `smashbox`, password stored as Fly secret `BASIC_AUTH_PASSWORD` |
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

### Rotating the auth password

```bash
fly secrets set BASIC_AUTH_PASSWORD=<new-password> --app smashbox
# Fly restarts the machine automatically when secrets change.
```

Setting `BASIC_AUTH_PASSWORD` to an empty string disables auth entirely — that's the local-dev default (so you don't have to log in on every reload), but **never set it empty in production**.

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

