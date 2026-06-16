# Build-once, run-anywhere image for Backtick. Mirrors the Render build/start:
# install deps, bump the service-worker cache version, then run uvicorn. Fully
# host-agnostic — point DATABASE_URL / DATA_CACHE_DIR at a mounted volume and the
# app self-initializes the SQLite schema on first boot (create_all).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so the layer caches across code-only changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Same step Render runs in buildCommand — keeps PWA clients from serving stale assets.
RUN python scripts/bump_sw_version.py

# SQLite DB + parquet cache live here; mount a persistent volume at this path in
# prod (Render disk, Fly volume, docker -v). Defaults are overridable via env.
ENV DATA_CACHE_DIR=/var/data/data_cache \
    DATABASE_URL=sqlite+aiosqlite:////var/data/backtick.db
VOLUME ["/var/data"]

EXPOSE 8000
# Honor $PORT when the platform injects one (Render/Fly), else default to 8000.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
