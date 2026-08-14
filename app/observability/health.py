"""
Liveness and readiness probes for the CIV-CON backend.

Two endpoints, both excluded from the OpenAPI schema because they
are operational surface, not public API:

- `GET /health`  — liveness. Cheap. Always 200 unless the process
  is dead. K8s `livenessProbe` should hit this.
- `GET /ready`   — readiness. Pings the database (`SELECT 1`) and
  Redis (`PING`) with bounded timeouts. 503 if any check fails.
  K8s `readinessProbe` should hit this.

The two-probe split matters: a transient DB blip should take the
pod out of load-balancer rotation (503 from /ready) but should
NOT kill the pod (K8s `livenessProbe` would restart on failure,
which makes a brief DB hiccup into a cascading restart loop).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.realtime import get_connection_manager
from app.database import get_db

logger = logging.getLogger("CIVCON.observability")

# Per-check timeouts — keep them well below the request budget so
# a slow probe doesn't eat into a real user's processing time.
_DB_TIMEOUT_S = 2.0
_REDIS_TIMEOUT_S = 1.0


health_router = APIRouter(include_in_schema=False)


@health_router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — the process is up and the event loop is alive.

    Intentionally does no IO. A K8s liveness probe should hit this
    every few seconds; a 200 means "do not restart the pod".
    """
    return {"status": "ok"}


@health_router.get("/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Readiness probe — DB and Redis are reachable.

    Returns 200 with `{"status": "ready", "checks": {...}}` when both
    checks succeed, or 503 with `{"status": "degraded", "checks":
    {...}}` when any check fails. A K8s readiness probe should hit
    this; a 503 means "take this pod out of LB rotation but do not
    kill it".

    Redis is treated as "ok" when the connection manager is in
    single-instance mode (no `REDIS_URL` configured) — that is the
    documented fallback behaviour.
    """
    checks: dict[str, bool] = {"db": False, "redis": False}

    # ---- DB ping ----
    try:
        await asyncio.wait_for(
            db.execute(text("SELECT 1")), timeout=_DB_TIMEOUT_S
        )
        checks["db"] = True
    except Exception as exc:
        logger.warning("Readiness: db ping failed: %s", exc)

    # ---- Redis ping ----
    manager = get_connection_manager()
    pub = getattr(manager, "_pub", None)
    if pub is None:
        # No Redis configured — running in single-instance mode.
        # Treat as healthy so dev environments without Redis still
        # pass the readiness probe.
        checks["redis"] = True
    else:
        try:
            await asyncio.wait_for(pub.ping(), timeout=_REDIS_TIMEOUT_S)
            checks["redis"] = True
        except Exception as exc:
            logger.warning("Readiness: redis ping failed: %s", exc)

    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ready" if ok else "degraded",
            "checks": checks,
        },
    )
