# Production container for Fly.io (or any Docker host).
#
# python:3.12-slim because 3.14 slim images are not yet stable across all base
# dependencies. requirements.txt uses lower-bound (`>=`) pins so it resolves
# fine on 3.12 too — see CLAUDE.md "Python 3.14 quirk" for the rationale.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: gcc + libpq for any wheel that builds from source (xlsxwriter,
# pydantic-core fallback). Trimmed after install to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

# Persistent volume on Fly mounts at /data — these env vars steer the app
# away from baking-in repo-relative paths.
ENV DATA_DIR=/data \
    UPLOAD_DIR=/data/uploads \
    EXPORT_DIR=/data/exports \
    PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
