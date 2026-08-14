# ============================================================================
# CIV-CON Backend — Production Dockerfile
# ============================================================================
# Multi-stage build:
#   - builder : resolves dependencies into a clean wheel layer
#   - runtime : slim image with only what's needed at runtime
#
# Pinned Python matches the project convention (Python 3.11). Versions
# of OS packages are intentionally left unpinned so `apt-get update`
# rolls security fixes on rebuild — pinning to a digest would require
# manual maintenance we don't want to own.
# ============================================================================

# ---- Stage 1: dependency resolver ----------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-time system deps for asyncpg, cryptography, psutil, etc.
# libpq-dev          → asyncpg + psycopg2-binary build wheels (some still need pg_config)
# gcc + libffi-dev   → cryptography + bcrypt source builds
# libxml2/libxslt    → lxml / xmlsec transitive deps
# pkg-config         → find the above libs
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        libffi-dev \
        libxml2-dev \
        libxslt1-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements first to leverage Docker layer cache — the
# requirements file changes less often than the source.
COPY requirements.txt .

# Install into a prefix we'll copy across stages. --prefix isolates
# the install from any system Python on the slim base.
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ---- Stage 2: runtime ----------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    WEB_CONCURRENCY=2 \
    # Drop privileges — never run as root in production.
    CIVCON_USER=civcon

# Runtime-only system deps:
#   libpq5       → asyncpg wheel runtime requirement
#   curl         → used by Render's healthcheck (curl /health)
#   tini         → PID 1 signal forwarding (clean shutdown of uvicorn workers)
#   ca-certificates → outbound TLS to Postgres / Sentry / Resend / etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user. UID 10001 is outside the typical "system"
# range so it doesn't clash with any host uid a volume might map to.
RUN groupadd --system --gid 10001 ${CIVCON_USER} \
    && useradd  --system --uid 10001 --gid ${CIVCON_USER} \
                --home-dir /app --shell /usr/sbin/nologin ${CIVCON_USER}

WORKDIR /app

# Copy the resolved Python packages from the builder. /install/lib is
# where pip --prefix puts site-packages on Debian-slim; /install/bin
# has console scripts (alembic, uvicorn).
COPY --from=builder /install/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /install/bin /usr/local/bin

# Copy application source.
COPY --chown=${CIVCON_USER}:${CIVCON_USER} app ./app
COPY --chown=${CIVCON_USER}:${CIVCON_USER} alembic ./alembic
COPY --chown=${CIVCON_USER}:${CIVCON_USER} alembic.ini ./alembic.ini
COPY --chown=${CIVCON_USER}:${CIVCON_USER} spam_detector.py ./spam_detector.py
COPY --chown=${CIVCON_USER}:${CIVCON_USER} init_db.py ./init_db.py

# Pre-create the runtime-only directories the app writes to. The
# non-root user owns them so the app doesn't need write access to
# anywhere outside /app.
RUN mkdir -p /app/nltk_data /app/static/uploads /app/Uploads /app/media \
    && chown -R ${CIVCON_USER}:${CIVCON_USER} /app

USER ${CIVCON_USER}

# Expose the uvicorn port. Render / most platforms inject PORT at
# runtime — uvicorn reads $PORT directly when started via the
# `start.sh` script (see below).
EXPOSE 8000

# Container-level healthcheck. Render and Docker Swarm respect
# HEALTHCHECK; k8s equivalents read /health via an httpGet probe.
# `--fail` makes curl exit non-zero on any non-2xx. The interval /
# timeout / retries are tuned for a 5-second liveness budget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:${PORT}/health || exit 1

# tini as PID 1 so SIGTERM (from Render's graceful shutdown) reaches
# uvicorn workers and they drain in-flight requests instead of being
# SIGKILL'd. The exec form (JSON array) is required so tini is PID 1.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Start uvicorn. WEB_CONCURRENCY defaults to 2 (one worker per CPU
# core on Render's free tier; bump via env var on larger plans).
# --proxy-headers makes uvicorn trust the X-Forwarded-* headers
# Render's load balancer injects (otherwise our CORS/origin checks
# would see the internal host header).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*' --log-level info"]