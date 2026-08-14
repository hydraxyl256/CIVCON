import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_limiter import FastAPILimiter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings

# ----- API production standards -----
from app.core.api_responses import ErrorDetail, ErrorResponse, code_for_status
from app.core.cache_headers import CacheControlMiddleware
from app.core.csrf import CsrfMiddleware
from app.core.logging_config import configure_logging
from app.core.middleware import (
    AccessLogMiddleware,
    AuthCacheMiddleware,
    RequestIdMiddleware,
    RequestSizeLimitMiddleware,
    RequestTimeoutMiddleware,
)
from app.core.realtime import (
    get_connection_manager,
)
from app.core.ws_auth import (
    WebSocketAuthError,
    authenticate_ws,
    close_ws_with_auth_error,
)

# Internal imports
from app.database import Base, engine, get_db

# ----- Observability -----
# Initialise Sentry BEFORE the logging config is installed so the
# Sentry logging integration can hook into the same handler we
# install next. The helper is a no-op when SENTRY_DSN is empty.
from app.observability import (
    PrometheusMiddleware,
    health_router,
    init_sentry,
    metrics_router,
)
from app.observability.metrics import ws_messages_total
from app.routers import (
    admin_analytics,
    admin_communication,
    admin_dashboard,
    admin_subscriptions,
    articles,
    auth,
    batch,
    cases,
    categories,
    comments,
    events,
    follow,
    groups,
    live_feeds,
    live_ws,
    moderation,
    notifications,
    posts,
    search,
    topics,
    uploads,
    users,
    vote,
)
from app.routers import (
    analytics as analytics_router,
)
from app.routers import (
    push as push_router,
)
from app.routers.ussd import router as ussd_router
from app.spam_detector import download_nltk_resources
from app.websockets import topics as topics_ws

init_sentry()


#  Logging Configuration
# Replaces the previous logging.basicConfig(...) call with the new
# structured-logging setup. JSON for production, text for local dev.
configure_logging(
    level=settings.log_level,
    log_format=settings.log_format,
)
logger = logging.getLogger("CIVCON")


#  FastAPI Application

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Modern FastAPI lifespan (replaces `@app.on_event("startup")`,
    which is deprecated since FastAPI 0.110).

    Initialises:
      - the DB schema (idempotent `Base.metadata.create_all`)
      - the WebSocket connection manager (heartbeat + optional
        Redis pub/sub subscriber)
      - the rate limiter
      - NLTK resources
    """
    # DB tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Database tables initialized")

    # WS connection manager
    manager = get_connection_manager(redis_url=settings.redis_url)
    await manager.start()
    logger.info("✅ Realtime manager ready")

    # Rate limiter
    try:
        _redis = redis_asyncio.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        await FastAPILimiter.init(_redis)
        logger.info("✅ Rate limiter (fastapi-limiter) initialized")
    except Exception as e:
        logger.warning(f"⚠️ Rate limiter could not be initialized: {e}")

    # NLTK
    try:
        download_nltk_resources()
        logger.info("✅ NLTK resources ready")
    except Exception as e:
        logger.warning(f"⚠️ NLTK resource setup failed: {e}")

    try:
        yield
    finally:
        # Stop the WS manager cleanly so it cancels the heartbeat
        # task and closes every active socket.
        manager = get_connection_manager()
        try:
            await manager.stop()
            logger.info("🛑 Realtime manager stopped cleanly")
        except Exception as e:
            logger.warning(f"⚠️ Realtime manager shutdown error: {e}")


app = FastAPI(
    title="CIVCON API",
    description=(
        "CIVCON enables Ugandan citizens to directly engage with their MPs "
        "on local issues, fostering transparency, accountability, and civic participation."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Use `ujson` (already pinned at `ujson==5.11.0`) for the default
# router response class. ujson is ~3-5× faster than the stdlib
# json encoder and produces ~5-10% smaller payloads (smaller
# numbers, fewer spaces). The exception handlers below still use
# `JSONResponse` because `UJSONResponse` doesn't accept the same
# `content=` shape in some edge cases (e.g. Pydantic v2 models
# with `ConfigDict(json_encoders=...)`).
from fastapi.responses import UJSONResponse

app.router.default_response_class = UJSONResponse

#  CORS Settings
origins = [
    "https://civ-con-sh2j.vercel.app",
    "https://civ-con-front.vercel.app",
    "http://localhost:5173",
    "https://civ-con.org",
    "https://app.civ-con.org"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# User-Agent capture for the push router. The push handlers read the
# caller UA via `push.current_user_agent_from_state()` so we can persist
# a `VARCHAR(255)` slice of it. Done in middleware (not per-handler)
# because we want to keep handlers dependency-only. The contextvar is
# scoped per request, so concurrent requests can't bleed.
from starlette.middleware.base import BaseHTTPMiddleware

from app.routers.push import set_current_user_agent


class UserAgentCaptureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        set_current_user_agent(request.headers.get("user-agent"))
        return await call_next(request)


app.add_middleware(UserAgentCaptureMiddleware)


# ----- Production-grade middleware stack -----
# Order matters: middlewares added LATER wrap middlewares added EARLIER
# (i.e. the last-added is the OUTERMOST). The stack below processes a
# request top-down in this order:
#
#   client -> RequestIdMiddleware        (outermost; sets request id)
#         -> AccessLogMiddleware         (emits access log line)
#         -> PrometheusMiddleware        (records per-request RED metrics)
#         -> RequestSizeLimitMiddleware  (rejects oversize bodies)
#         -> RequestTimeoutMiddleware    (caps processing time)
#         -> CORSMiddleware              (existing)
#         -> SessionMiddleware           (existing)
#         -> router
#
# RequestIdMiddleware must be the OUTERMOST so every other middleware
# and the exception handlers can read the request id. PrometheusMiddleware
# sits just inside AccessLogMiddleware so the response status it records
# matches the status the access log emits.
app.add_middleware(
    RequestTimeoutMiddleware,
    timeout_seconds=settings.request_timeout_seconds,
)
app.add_middleware(
    RequestSizeLimitMiddleware,
    max_body_bytes=settings.max_request_body_bytes,
)
# CacheControlMiddleware attaches ETag + Cache-Control on GET responses
# and short-circuits If-None-Match hits to 304 Not Modified. Placed
# inside the timeout middleware so a slow handler is bounded, and
# inside PrometheusMiddleware so cache hits still record metrics.
app.add_middleware(CacheControlMiddleware)
# GZipMiddleware wraps the routers (innermost). Inserted after the
# size-limit and timeout middlewares so a malicious oversize payload
# is rejected before any gzip attempt, and so a slow handler is
# terminated before we waste CPU compressing a response that will
# never be sent. `minimum_size=1000` — small JSON envelopes cost
# more to compress than they save (CPU vs. payload).
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(PrometheusMiddleware)
app.add_middleware(AccessLogMiddleware)
app.add_middleware(AuthCacheMiddleware)
# CSRF protection for cookie-based auth (F-008). Mounted inside the
# access-log middleware so a rejected request is still visible in the
# log, and outside the cache middleware so the per-request user cache
# is still cleared for rejected requests.
app.add_middleware(CsrfMiddleware)
app.add_middleware(RequestIdMiddleware)  # outermost


#  Session Middleware
#
# SECURITY (F-004): The previous version of this code fell back to a
# hardcoded constant ("supersecret_session_key") whenever
# settings.session_secret_key was empty. That fallback is a latent
# footgun: if the env var is ever missing in production, all sessions
# become forgeable by anyone who has read the source code (and the
# authlib OAuth `state` parameter, which is signed by this key, would
# no longer protect the callback redirect).
#
# We now fail loud and early instead of silently downgrading:
#   1. pydantic-settings requires `session_secret_key: str` with no
#      default, so the app refuses to boot if the env var is unset.
#   2. The `or "…"` fallback is removed here.
#   3. In any non-development environment we additionally reject known
#      placeholders to prevent ops from shipping the example value.
if (
    settings.environment.lower() != "development"
    and settings.session_secret_key.lower() in {
        "supersecret_session_key",
        "change_me",
        "changeme",
        "",
    }
):
    raise RuntimeError(
        "SESSION_SECRET_KEY is unset or set to a known placeholder. "
        "Refusing to start in a non-development environment. "
        "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    session_cookie="civcon_session",
    same_site="lax",
    https_only=settings.environment.lower() != "development",
)


# ============================================================================
# Global exception handlers
# ============================================================================
# Every non-2xx response is normalised to the standard `ErrorResponse`
# envelope so the frontend's existing `err.response.data.detail` read
# path keeps working (the `detail` field is always a non-empty string).
#
# The status code is propagated unchanged from the original exception
# — the global handler only changes the *shape* of the response, not
# the *cause* of the error.
# ----------------------------------------------------------------------------


def _request_id_of(request: Request) -> str:
    """Return the request id set by RequestIdMiddleware, or empty string."""
    return getattr(request.state, "request_id", "") or ""


def _error_payload(
    *,
    request: Request,
    status_code: int,
    detail: str,
    code: str | None = None,
    errors: list[ErrorDetail] | None = None,
    hint: str | None = None,
) -> dict:
    """Build the standard error envelope as a dict for JSONResponse."""
    return ErrorResponse(
        detail=detail,
        code=code or code_for_status(status_code),
        request_id=_request_id_of(request),
        errors=errors or [],
        hint=hint,
    ).model_dump()


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render FastAPI's HTTPException as the standard error envelope.

    Preserves any headers attached to the exception (notably
    `WWW-Authenticate` for 401s and `Retry-After` for 429s).
    """
    detail = exc.detail
    # FastAPI passes the detail as the literal string for our routers,
    # but historically it has also been a dict/iterable. Coerce to a
    # single string so the envelope's `detail` is always a string.
    if not isinstance(detail, str):
        detail = str(detail)
    if not detail:
        detail = "An error occurred."
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            request=request,
            status_code=exc.status_code,
            detail=detail,
        ),
        headers=exc.headers,
    )


# Starlette's own HTTPException (e.g. 404 from an unmatched route) is NOT
# a FastAPI HTTPException, so we register the same handler for the base
# class. FastAPI's HTTPException is a subclass of Starlette's, so the
# `StarletteHTTPException` registration alone would be enough — but we
# keep both for explicitness and so the FastAPI-specific subclass is
# matched first by FastAPI's dispatch.
@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render Starlette-raised HTTPExceptions (404/405/etc.) as the envelope."""
    detail = exc.detail
    if not isinstance(detail, str):
        detail = str(detail)
    if not detail:
        detail = "An error occurred."
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            request=request,
            status_code=exc.status_code,
            detail=detail,
        ),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render Pydantic v2 validation errors as the standard envelope.

    The `errors` field is a list of `{field, message, code}` records so
    the frontend can highlight the offending form fields.
    """
    field_errors: list[ErrorDetail] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        # Skip the "body" / "query" / "path" prefix that Pydantic adds so
        # the field path reads like "password" rather than "body.password"
        # for body-validated requests. Top-level "body" is dropped.
        field_parts = [str(p) for p in loc if p not in {"body", "query", "path"}]
        field = ".".join(field_parts) or "<unknown>"
        field_errors.append(
            ErrorDetail(
                field=field,
                message=err.get("msg", "Invalid value"),
                code=err.get("type"),
            )
        )

    # Compose a top-level detail string. The first error is usually the
    # most useful, but a join keeps the full picture for logs.
    if field_errors:
        first = field_errors[0]
        detail = f"Validation error: {first.field} — {first.message}"
    else:
        detail = "Validation error."

    return JSONResponse(
        status_code=422,
        content=_error_payload(
            request=request,
            status_code=422,
            detail=detail,
            code="validation_error",
            errors=field_errors,
            hint="Correct the highlighted fields and retry.",
        ),
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(
    request: Request, exc: IntegrityError
) -> JSONResponse:
    """Map SQLAlchemy IntegrityError to a 409.

    Unique-violation is the most common case (e.g. duplicate email on
    signup, duplicate like on the same post). Other constraint
    violations are still 409 — a 500 would be misleading.
    """
    logger.warning(
        "IntegrityError on %s %s: %s",
        request.method,
        request.url.path,
        exc.orig,
    )
    return JSONResponse(
        status_code=409,
        content=_error_payload(
            request=request,
            status_code=409,
            detail="A record with these values already exists.",
            code="conflict",
            hint="Change the conflicting values and retry.",
        ),
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Render ValueError as a 400."""
    return JSONResponse(
        status_code=400,
        content=_error_payload(
            request=request,
            status_code=400,
            detail=str(exc) or "Bad request.",
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Last-resort handler: log the full traceback and return a 500.

    IMPORTANT: this only fires for exceptions that aren't HTTPException,
    RequestValidationError, IntegrityError, or ValueError — those have
    their own handlers. The catch-all should be quiet in the response
    (no stack trace leaked) but loud in the logs.
    """
    rid = _request_id_of(request) or "<no-request-id>"
    logger.exception(
        "Unhandled exception on %s %s (request_id=%s): %s",
        request.method,
        request.url.path,
        rid,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content=_error_payload(
            request=request,
            status_code=500,
            detail="An internal error occurred. Please try again later.",
            hint="If this persists, contact support with the request id.",
        ),
    )


#  Realtime connection manager
# The singleton is created lazily in `lifespan` above so the same
# instance is reused by every router. Use `get_connection_manager()`
# at request time (never cache the result in a module-level constant)
# so the manager stays a single, observable object.
REDIS_URL = settings.redis_url


#  Include Routers (API Modules)

app.include_router(users.router)
app.include_router(posts.router)
app.include_router(auth.router)
app.include_router(vote.router)
app.include_router(search.router)
app.include_router(comments.router)
app.include_router(categories.router)
app.include_router(groups.router)
app.include_router(notifications.router)
app.include_router(live_feeds.router)
app.include_router(live_ws.router)
app.include_router(articles.router)
app.include_router(uploads.router)
app.include_router(topics.router)
app.include_router(topics_ws.router)
app.include_router(follow.router)
app.include_router(events.router)
app.include_router(batch.router)
app.include_router(analytics_router.router)
app.include_router(admin_analytics.router)
app.include_router(admin_dashboard.router)
app.include_router(admin_subscriptions.router)
app.include_router(moderation.router)
app.include_router(admin_communication.router)
app.include_router(ussd_router)
app.include_router(cases.router)  # /cases/{id}/timeline, /cases/{id}/status
app.include_router(push_router.router)

# Observability routers. Both are operational endpoints, not public
# API surface, and are excluded from the OpenAPI schema at the
# router level.
app.include_router(health_router)  # /health (liveness), /ready (readiness)
if settings.metrics_enabled:
    app.include_router(metrics_router)  # /metrics (Prometheus exposition)


#  Root Endpoint
@app.get("/")
def root():
    return {
        "message": "Welcome to CIVCON API ",
        "status": "running",
        "version": "1.0.0",
       
    }



# WebSocket for notifications
@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str | None = None, db: AsyncSession = Depends(get_db)):
    """Real-time notifications stream.

    Migrated to the unified manager (`app.core.realtime`):
      - Auth runs through `authenticate_ws` (semantic close
        codes 4401/4403 on failure instead of 1008).
      - The manager's heartbeat loop replaces the previous
        `await websocket.receive_text()` keepalive, which
        blocked forever if the client never sent anything.
      - The `connect(websocket, user_id)` argument order is
        correct here (it was swapped in the old version).
    """
    try:
        current_user = await authenticate_ws(
            token, db, cookies=websocket.cookies
        )
    except WebSocketAuthError as err:
        await close_ws_with_auth_error(websocket, err)
        return

    manager = get_connection_manager()
    await manager.connect(current_user.id, websocket, route="/ws/notifications")
    try:
        while True:
            # Block on a single receive. The client's pong frame
            # updates `last_pong_at`; the manager's heartbeat loop
            # closes the socket if no pong arrives within
            # `HEARTBEAT_TIMEOUT_S`.
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            manager.mark_pong(current_user.id)
            # Inbound message metric.
            try:
                ws_messages_total.labels(
                    route="/ws/notifications", direction="in",
                ).inc()
            except Exception:
                pass
            # Don't echo — notifications are server-push only —
            # but accept pong frames silently.
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and obj.get("type") == "pong":
                        # Already marked above; nothing else to do.
                        continue
                except Exception:
                    pass
    finally:
        await manager.disconnect(current_user.id, websocket)


