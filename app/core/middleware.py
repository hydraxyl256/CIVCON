"""
Production-grade middleware for the CIV-CON API.

Middlewares registered in `main.py` (outermost first):

    1. RequestIdMiddleware       — assigns and propagates a request id
    2. AuthCacheMiddleware       — clears the per-request auth cache
    3. AccessLogMiddleware       — emits one access log line per request
    4. PrometheusMiddleware      — records RED metrics (in app/observability)
    5. RequestSizeLimitMiddleware — rejects oversized request bodies
    6. RequestTimeoutMiddleware  — caps request processing time

Each middleware also emits a single structured access-log line on
response so the platform log shipper can index per-request metrics
(method, path, status, duration_ms, request_id, client_ip).

WebSocket scopes are passed through without applying the size limit
or timeout — WebSockets are long-lived by design and would otherwise
be disconnected after 30 seconds.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import settings
from app.core.api_responses import ErrorResponse
from app.core.logging_config import access_log, set_request_id
from app.dependencies.auth import clear_request_user_cache

# Dedicated access logger; the message format is built by `access_log()`.
ACCESS_LOGGER_NAME = "CIVCON.access"
_access_logger = logging.getLogger(ACCESS_LOGGER_NAME)


# ============================================================================
# RequestIdMiddleware
# ============================================================================


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a UUIDv4 to every incoming request, propagate it everywhere.

    - If the client sends `X-Request-Id`, that value is reused (so an
      upstream proxy or the frontend can correlate logs across services).
    - Otherwise a fresh UUIDv4 is generated.
    - The id is set on `request.state.request_id` AND on the
      `request_id_var` ContextVar (so log lines emitted during the
      request automatically include it).
    - The id is echoed on the response as `X-Request-Id`.
    - Works for both HTTP and WebSocket scopes.
    """

    HEADER_NAME = "X-Request-Id"

    def __init__(self, app: ASGIApp, header_name: str = HEADER_NAME) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(self.header_name)
        # Defensive: a client could send a malicious value; cap length
        # and require printable ASCII so log lines stay well-formed.
        if incoming and len(incoming) <= 128 and all(
            32 <= ord(c) < 127 for c in incoming
        ):
            request_id = incoming
        else:
            request_id = uuid.uuid4().hex

        # Stash for downstream handlers / routers.
        request.state.request_id = request_id
        set_request_id(request_id)
        # Tag the Sentry event with the request id so any error
        # reported during this request is correlatable with the
        # X-Request-Id response header. Best-effort: silently no-op
        # if Sentry is not installed / not initialised.
        try:
            import sentry_sdk
            sentry_sdk.set_tag("request_id", request_id)
        except Exception:
            pass

        try:
            response = await call_next(request)
        finally:
            # Clear the contextvar so a stray log line after the response
            # isn't tagged with the previous request's id.
            set_request_id(None)

        response.headers[self.header_name] = request_id
        return response


# ============================================================================
# AuthCacheMiddleware
# ============================================================================


class AuthCacheMiddleware:
    """
    Reset the per-request auth cache at the start of every request.

    The per-request cache stores validated `User` rows keyed by their
    access token so admin pages that fan out to 4-8 endpoints don't
    re-query Postgres / Redis on each one. The cache is held in a
    `ContextVar`, which is per-task — but Starlette may reuse the same
    task across requests in some pools, so we explicitly clear it here
    to prevent stale users from leaking between requests.

    Pure ASGI middleware (no `BaseHTTPMiddleware`) so it doesn't add a
    task hop to the request lifecycle.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            clear_request_user_cache()
        await self.app(scope, receive, send)


# ============================================================================
# RequestSizeLimitMiddleware
# ============================================================================


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared `Content-Length` exceeds the limit.

    Only checks the `Content-Length` header (not the streamed body),
    which is safe for JSON / form-data / multipart requests — Starlette
    will buffer those bodies server-side, so a too-large body would
    already waste resources before reaching any router.

    A future hardening pass could add a streaming-body guard, but that
    requires buffering the body inside the middleware, which is a
    larger refactor.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        super().__init__(app)
        self.max_body_bytes = max_body_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # WebSockets and other non-HTTP scopes are passed through.
        if request.scope.get("type") != "http":
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                cl = int(content_length)
            except ValueError:
                cl = -1
            if cl > self.max_body_bytes:
                request_id = getattr(request.state, "request_id", "") or ""
                return JSONResponse(
                    status_code=413,
                    content=ErrorResponse(
                        detail=(
                            f"Request body of {cl} bytes exceeds the "
                            f"maximum allowed size of {self.max_body_bytes} bytes."
                        ),
                        code="payload_too_large",
                        request_id=request_id,
                        hint="Reduce the size of the request body.",
                    ).model_dump(),
                )
        return await call_next(request)


# ============================================================================
# RequestTimeoutMiddleware
# ============================================================================


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Enforce a per-request processing time budget.

    Uses `asyncio.wait_for` around the downstream call. On timeout,
    returns a 504 with the standard error envelope. WebSocket scopes
    are passed through without a timeout — WebSockets are long-lived by
    design and timing them out would break the notification / chat
    channels.
    """

    def __init__(self, app: ASGIApp, timeout_seconds: float) -> None:
        super().__init__(app)
        self.timeout_seconds = timeout_seconds

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Pass through WebSockets and lifespan scopes unchanged.
        if request.scope.get("type") != "http":
            return await call_next(request)

        request_id = getattr(request.state, "request_id", "") or ""
        try:
            return await asyncio.wait_for(
                call_next(request), timeout=self.timeout_seconds
            )
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content=ErrorResponse(
                    detail=(
                        f"Request exceeded the {self.timeout_seconds}s processing budget."
                    ),
                    code="request_timeout",
                    request_id=request_id,
                    hint="Simplify the request and retry.",
                ).model_dump(),
            )


# ============================================================================
# AccessLogMiddleware
# ============================================================================


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit a single structured access-log line per HTTP request.

    Must be installed INSIDE `RequestIdMiddleware` so it can read the
    request id, but OUTSIDE the size / timeout middlewares so it can
    still log the response for rejected / timed-out requests.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Pass through WebSockets — they have their own connection log.
        if request.scope.get("type") != "http":
            return await call_next(request)

        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            client_ip = self._client_ip(request)
            access_log(
                _access_logger,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=duration_ms,
                client_ip=client_ip,
                user_agent=request.headers.get("user-agent"),
            )
            # Slow-request warning: emit a separate WARNING line so a
            # log shipper can alert on slow paths without having to
            # parse the duration_ms out of every access log line.
            # Throttled by the configured threshold (default 1000 ms).
            threshold = settings.slow_request_threshold_ms
            if duration_ms > threshold:
                _access_logger.warning(
                    "slow_request method=%s path=%s status=%d "
                    "duration_ms=%.2f threshold_ms=%.0f client_ip=%s",
                    request.method,
                    request.url.path,
                    status,
                    duration_ms,
                    threshold,
                    client_ip or "-",
                )

    @staticmethod
    def _client_ip(request: Request) -> str | None:
        """Resolve the client IP, honouring X-Forwarded-For if present."""
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Use the leftmost address — the original client.
            return xff.split(",", 1)[0].strip() or None
        if request.client:
            return request.client.host
        return None
