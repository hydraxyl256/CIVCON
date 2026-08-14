"""
CacheControlMiddleware — ETag + Cache-Control headers for read endpoints.

Goals:
- Compute a weak ETag (sha1 of the JSON body, truncated to 16 chars)
  on every read-only GET response.
- Set `Cache-Control: private, max-age=N, must-revalidate` where
  the route pattern matches a configured rule, otherwise `no-store`.
- Honour inbound `If-None-Match`: if the etag matches, return
  `304 Not Modified` instead of the body — saves bandwidth on
  repeated fetches of the same resource.
- Record metrics: `civcon_http_cache_hits_total{route=...}` /
  `civcon_http_cache_misses_total{route=...}`.

Behaviour-preservation guarantees (per the api_optimization brief):
- This middleware is purely additive: it adds response headers
  and a 304 short-circuit. It NEVER modifies the response body,
  the response status (other than 200 → 304 on a matching
  If-None-Match), or the response field names.
- Existing clients that don't read ETag / Cache-Control continue
  to work unchanged.

Cardinality control:
- The `route` label is the matched route template
  (e.g. `/posts/{post_id}`), not the literal path. A flood of
  404s on `/foo/123` won't explode the metrics cardinality.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.metrics import (
    http_cache_hits_total,
    http_cache_misses_total,
)

logger = logging.getLogger("CIVCON.cache")


# Default cache durations by route pattern. Patterns are matched as
# regexes against the matched route template (`request.scope["route"].path`).
# The first matching pattern wins; the default (`<fallback>`) is no-store.
#
# A pattern of `0` means "do not cache" (sets `Cache-Control: no-store`).
DEFAULT_RULES: list[tuple[str, int]] = [
    # Static-ish: districts / categories
    (r"^/auth/locations/.*", 86_400),    # 1 day
    (r"^/categories/?$", 300),           # 5 min
    # User content with mild churn
    (r"^/users/me/?$", 0),               # never cache (auth context)
    (r"^/users/[^/]+/?$", 60),           # 1 min (profile pages)
    (r"^/users/?$", 30),
    # Feed / content with frequent change
    (r"^/posts/?$", 30),
    (r"^/posts/[^/]+/?$", 30),
    (r"^/topics/?$", 30),
    (r"^/topics/[^/]+/?$", 30),
    (r"^/articles/?$", 60),
    (r"^/articles/[^/]+/?$", 60),
    (r"^/search/?$", 0),                 # search results are per-query
    (r"^/notifications/?$", 0),          # user-private, live
    # Fallback: no cache
    (r".*", 0),
]


def _route_template(request: Request) -> str:
    """Return the matched route template (e.g. `/posts/{post_id}`).

    Falls back to the literal path (clamped to 128 chars) when no
    route matched, so a 404 still has a bounded label value.
    """
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    raw = request.url.path
    return raw[:128] if raw else "<empty>"


def _max_age_for(route_template: str, rules: list[tuple[str, int]]) -> int:
    """Return the configured max-age for a given route template."""
    for pattern, max_age in rules:
        if re.match(pattern, route_template):
            return max_age
    return 0


def _weak_etag(body: bytes) -> str:
    """Compute a weak ETag from the response body.

    A weak ETag (prefixed with `W/`) is semantically equivalent for
    our purposes — we're not doing byte-range requests, just
    "did the resource change?" detection.
    """
    digest = hashlib.sha1(body).hexdigest()[:16]
    return f'W/"{digest}"'


class CacheControlMiddleware(BaseHTTPMiddleware):
    """Add ETag + Cache-Control to read-only GET responses.

    Skips:
      - Non-HTTP scopes (WebSockets, lifespan).
      - Non-GET requests (writes are never cached).
      - Responses that already carry a `Cache-Control` header set by
        the handler (the handler's intent wins).
      - Non-2xx responses (don't cache errors; clients should retry).
      - Streaming responses (no body buffered).
    """

    def __init__(
        self,
        app,
        rules: list[tuple[str, int]] | None = None,
    ) -> None:
        super().__init__(app)
        self.rules = rules or DEFAULT_RULES

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # WebSocket + lifespan pass through unchanged.
        if request.scope.get("type") != "http":
            return await call_next(request)

        # Only cache read-only GETs.
        if request.method.upper() != "GET":
            return await call_next(request)

        route_template = _route_template(request)

        # 1. Check inbound If-None-Match BEFORE running the handler
        #    so we can short-circuit cleanly. We don't yet know the
        #    etag (it depends on the body), so we have to actually
        #    run the handler once to learn the etag. Then on the
        #    second request, the client sends the etag and we can
        #    respond 304.
        response = await call_next(request)

        # Only cache successful responses.
        if response.status_code != 200:
            return response

        # Handler already set Cache-Control — respect it.
        if "cache-control" in {k.lower(): None for k in response.headers}:
            return response

        # Streamed responses can't have a meaningful etag (body is
        # not yet fully buffered when send is called).
        if getattr(response, "body_iterator", None) is not None and not getattr(
            response, "body", None
        ):
            # Try to read body_iterator into bytes once.
            try:
                chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    if isinstance(chunk, str):
                        chunks.append(chunk.encode("utf-8"))
                    else:
                        chunks.append(chunk)
                body = b"".join(chunks)
            except Exception:
                # If we can't buffer the body, don't try to cache it.
                return response
            # Replace the streamed body with a single byte buffer so
            # the rest of the pipeline (gzip etc.) can read it.
            response = Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        body = getattr(response, "body", None)
        if not isinstance(body, bytes) or not body:
            # Empty body — nothing meaningful to etag.
            return response

        # 2. Compute ETag and check If-None-Match.
        etag = _weak_etag(body)
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match == etag:
            # 304 Not Modified — empty body, just the headers.
            try:
                http_cache_hits_total.labels(route=route_template).inc()
            except Exception:
                pass
            not_modified = Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": _build_cache_control(self.rules, route_template),
                    "X-Cache": "HIT",
                },
            )
            return not_modified

        # 3. Cache miss — attach headers to the response.
        try:
            http_cache_misses_total.labels(route=route_template).inc()
        except Exception:
            pass
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = _build_cache_control(self.rules, route_template)
        response.headers["X-Cache"] = "MISS"
        return response


def _build_cache_control(rules: list[tuple[str, int]], route_template: str) -> str:
    """Build the `Cache-Control` value for a route template.

    Returns:
      - `private, max-age=N, must-revalidate` when a positive max-age
        matches.
      - `private, no-store` when the matched rule is 0 (no cache) —
        we still mark `private` because the response carries
        per-user content even when not cached.
    """
    max_age = _max_age_for(route_template, rules)
    if max_age <= 0:
        return "private, no-store"
    return f"private, max-age={max_age}, must-revalidate"
