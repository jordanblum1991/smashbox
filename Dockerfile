# Production container for Fly.io (or any Docker host).
#
# Stage 1 compiles Tailwind from the templates so the CSS can never go stale
# in production: every `fly deploy` rebuilds it from the current templates,
# independent of whether anyone ran `npm run css` locally. The compiled
# stylesheet is copied into the Python image in stage 2. (The app references
# /static/css/tailwind.css and is no longer on the Tailwind Play CDN.)
FROM node:24-slim AS css
WORKDIR /build
# Only the files Tailwind needs — keeps this layer cached unless the build
# config, the lockfile, or the scanned content (templates / JS) actually change.
COPY package.json package-lock.json tailwind.config.js tailwind.input.css ./
COPY app/templates ./app/templates
COPY app/static/js ./app/static/js
RUN npm ci && npm run css
# -> emits /build/app/static/css/tailwind.css (per the "css" script in package.json)

# python:3.12-slim because 3.14 slim images are not yet stable across all base
# dependencies. requirements.txt uses lower-bound (`>=`) pins so it resolves
# fine on 3.12 too — see CLAUDE.md "Python 3.14 quirk" for the rationale.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps:
#   - gcc + libpq: for any wheel that builds from source (xlsxwriter,
#     pydantic-core fallback).
#   - libpango / libpangoft2 / libharfbuzz / libcairo / libffi: WeasyPrint's
#     runtime (PDF rendering for /admin/invoices/{id}/pdf).
#   - shared-mime-info: MIME-type resolution for WeasyPrint.
#   - fonts-dejavu-core + fonts-dejavu-extra: DejaVu Sans family — `-core`
#     ships Regular and Bold; `-extra` adds Italic and Bold Italic, which
#     the invoice template uses for the subtitle and footer.
# Apt lists trimmed after install to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc libpq-dev \
      libpango-1.0-0 libpangoft2-1.0-0 \
      libharfbuzz0b libcairo2 \
      libffi-dev \
      shared-mime-info fonts-dejavu-core fonts-dejavu-extra \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
# Alembic config + migration scripts — needed by the fly.toml release_command
# (`alembic upgrade head`), which runs from this WORKDIR (/app). alembic.ini's
# script_location = alembic resolves relative to here.
COPY alembic.ini ./
COPY alembic ./alembic

# Compiled Tailwind from stage 1. Copied AFTER `COPY app` so it lands beside
# the hand-written app.css rather than being clobbered by it. The dev tree's
# tailwind.css (if present) is overwritten with this freshly-built one.
COPY --from=css /build/app/static/css/tailwind.css ./app/static/css/tailwind.css

# Persistent volume on Fly mounts at /data — these env vars steer the app
# away from baking-in repo-relative paths.
ENV DATA_DIR=/data \
    UPLOAD_DIR=/data/uploads \
    EXPORT_DIR=/data/exports \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
