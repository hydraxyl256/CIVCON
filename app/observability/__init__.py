"""
Observability surfaces for the CIV-CON backend.

This package contains the runtime integrations that turn the
application into a service an operator can monitor:

- Sentry (error tracking) — initialised from `init_sentry()`.
- Prometheus (metrics) — `metrics_router` and `PrometheusMiddleware`.
- Liveness + readiness probes — `health_router`.

Everything in this package is opt-in via env vars (`SENTRY_DSN`,
`METRICS_ENABLED`, etc.). With nothing set the app runs with the
same behaviour it had before this package was added.
"""
from app.observability.health import health_router
from app.observability.metrics import PrometheusMiddleware, metrics_router
from app.observability.sentry import init_sentry

__all__ = [
    "PrometheusMiddleware",
    "health_router",
    "init_sentry",
    "metrics_router",
]
