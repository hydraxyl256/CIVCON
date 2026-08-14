"""
Prometheus metrics for the CIV-CON backend.

Exposes the four standard RED + in-flight metrics for every HTTP
request, plus the `/metrics` endpoint itself. Counters and the
histogram that already exist in `app/routers/ussd.py` and
`app/spam_detector.py` are picked up automatically because
`prometheus_client` keeps a global registry.

Cardinality control
-------------------
The `path` label is taken from `request.scope["route"].path` (the
route template, e.g. `/posts/{post_id}`) when a route is matched.
For unmatched routes (404s) the literal `request.url.path` is used,
clamped to 128 characters to keep a single 404-flooding client
from creating an unbounded set of label values.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger("CIVCON.observability")


# ============================================================================
# Metric definitions
# ============================================================================


# Total HTTP requests handled. Labelled by method, path template, status.
http_requests_total = Counter(
    "civcon_http_requests_total",
    "Total HTTP requests handled, labelled by method, path, status.",
    labelnames=("method", "path", "status"),
)

# Request duration in seconds. The bucket boundaries cover everything
# from a sub-5ms cache hit to a near-timeout 30s request.
http_request_duration_seconds = Histogram(
    "civcon_http_request_duration_seconds",
    "HTTP request duration in seconds, labelled by method, path, status.",
    labelnames=("method", "path", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# Number of HTTP requests currently in flight.
http_requests_in_flight = Gauge(
    "civcon_http_requests_in_flight",
    "Number of HTTP requests currently being processed.",
)


# ============================================================================
# WebSocket metrics
# ============================================================================
#
# The Prometheus middleware above only sees HTTP scopes, so we
# instrument the connection manager directly. Counters are
# incremented from `app.core.realtime` at connect/disconnect/send/
# close time, with `route` and `direction`/`reason` labels kept
# bounded to a small allow-list (the route table).
#
# Cardinality control:
# - `route` label is bounded — only the three kept WS route names
#   ever appear (`/ws/notifications`, `/ws/topics`,
#   `/ws/live/{feed_id}`). The manager strips path parameters
#   before labelling.
# - `reason` is also bounded (`abnormal_close`, `heartbeat_timeout`,
#   `auth_failed`, `explicit_close`).


# Current open connections per route.
ws_connections = Gauge(
    "civcon_ws_connections",
    "Number of open WebSocket connections, labelled by route.",
    labelnames=("route",),
)

# Cumulative connect/disconnect events.
# NB: prometheus_client strips the `_total` suffix from Counter names,
# which would collide with the `ws_connections` Gauge above. Use
# `events_total` instead — the metric still ends in `_total` after
# the strip and reads naturally as a Counter.
ws_connections_total = Counter(
    "civcon_ws_connections_events_total",
    "Cumulative WebSocket connect/disconnect events, labelled by route and event.",
    labelnames=("route", "event"),
)

# Cumulative inbound/outbound frames.
ws_messages_total = Counter(
    "civcon_ws_messages_events_total",
    "Cumulative WebSocket frames, labelled by route and direction.",
    labelnames=("route", "direction"),
)

# Cumulative reconnect attempts, labelled by trigger.
ws_reconnect_total = Counter(
    "civcon_ws_reconnect_attempts_total",
    "Cumulative WebSocket reconnect attempts, labelled by route and reason.",
    labelnames=("route", "reason"),
)


# ============================================================================
# HTTP cache + compression + batch metrics (Phase E)
# ============================================================================
#
# Cardinality control: `route` here is the matched route template
# (e.g. `/posts/{post_id}`) — same low-cardinality discipline as the
# RED metrics above. `status_class` for the batch endpoint is a
# coarse-grained bucket (`2xx`, `4xx`, `5xx`) — never an unbounded
# raw status code.


# ETag matches: GET requests whose `If-None-Match` matched the
# current response and were answered with `304 Not Modified`.
http_cache_hits_total = Counter(
    "civcon_http_cache_hits_total",
    "HTTP responses served from the in-process cache (304 Not Modified).",
    labelnames=("route",),
)

# ETag misses: GET responses with a fresh ETag attached.
http_cache_misses_total = Counter(
    "civcon_http_cache_misses_total",
    "HTTP responses with a fresh ETag attached (no client cache hit).",
    labelnames=("route",),
)

# Compression ratio (compressed_size / uncompressed_size). 1.0 = no
# compression applied. Buckets cover `no compression` through ~10×
# compression (0.1 = 90% saving).
http_compression_ratio = Histogram(
    "civcon_http_compression_ratio",
    "Ratio of compressed payload size to uncompressed size (1.0 = no compression).",
    labelnames=("route",),
    buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# Sub-request count per batch request.
http_batch_size = Histogram(
    "civcon_http_batch_size",
    "Number of sub-requests per /api/batch call.",
    buckets=(1, 2, 5, 10, 15, 20),
)

# Sub-request failures inside a batch — a per-item 4xx/5xx, NOT
# the parent batch status (the batch itself always returns 200
# unless the parent request itself fails).
http_batch_subrequest_failures_total = Counter(
    "civcon_http_batch_subrequest_failures_total",
    "Cumulative sub-request failures inside /api/batch, by status class.",
    labelnames=("status_class",),
)


# ============================================================================
# Middleware
# ============================================================================


def _path_label(request: Request) -> str:
    """Return a bounded, low-cardinality path label for the request.

    - If a route matched, use the route template (e.g.
      `/posts/{post_id}`) so every post id shares the same series.
    - Otherwise, use the literal path clamped to 128 characters.
    """
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    raw = request.url.path
    return raw[:128] if raw else "<empty>"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Record per-request RED metrics for every HTTP request.

    WebSocket scopes are passed through (they have their own
    observability in the connection manager).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.scope.get("type") != "http":
            return await call_next(request)

        method = request.method
        path = _path_label(request)
        http_requests_in_flight.inc()
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_s = time.perf_counter() - start
            status = str(status_code)
            try:
                http_requests_total.labels(
                    method=method, path=path, status=status
                ).inc()
                http_request_duration_seconds.labels(
                    method=method, path=path, status=status
                ).observe(duration_s)
            except Exception as exc:
                # Never let a metrics-write failure break the request.
                logger.warning("PrometheusMiddleware: failed to record metrics: %s", exc)
            finally:
                http_requests_in_flight.dec()


# ============================================================================
# /metrics endpoint
# ============================================================================


metrics_router = APIRouter(include_in_schema=False)


@metrics_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition endpoint.

    Returns the current snapshot of every metric registered with the
    default `prometheus_client` registry, including the application
    counters defined in `app/routers/ussd.py` and `app/spam_detector.py`.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
