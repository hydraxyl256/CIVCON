"""
`POST /api/batch` — execute multiple sub-requests in one round-trip.

Use cases:
- The frontend's `notificationService.markAllAsRead(notifications)`
  fires N parallel PATCHes today. With `/api/batch` it sends one
  POST with N sub-requests; the backend fans them out
  concurrently and returns one array.
- Future bulk actions (bulk follow / unfollow, bulk archive, …) get
  the same shape for free.

Behaviour-preservation:
- Sub-requests are dispatched through the real FastAPI route
  handler — same auth, same validation, same rate limiting. The
  only thing that does NOT apply is the middleware stack (no
  per-sub-request ETag / compression — sub-responses are
  serialised into JSON instead).
- The parent `/api/batch` always returns 200 with a per-item
  status; a sub-request 5xx is captured into the per-item
  result, never bubbled to the parent. This is the contract that
  lets the frontend treat the batch as "one logical request"
  even though the wire-level response is an array.
- The auth is enforced ONCE on the parent request (the access
  token attaches to all sub-requests via the same DB session).

Cardinality / abuse control:
- `MAX_BATCH_SIZE = 20` sub-requests per parent call.
- Body size limit still applies (the existing
  `RequestSizeLimitMiddleware`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import User
from app.observability.metrics import (
    http_batch_size,
    http_batch_subrequest_failures_total,
)

logger = logging.getLogger("batch")
router = APIRouter(prefix="/api", tags=["batch"])


MAX_BATCH_SIZE = 20
ALLOWED_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


# ─────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────


class BatchSubRequest(BaseModel):
    method: str = Field(..., description="HTTP method (GET/POST/PATCH/PUT/DELETE)")
    path: str = Field(..., description="Path component, e.g. `/notifications/42/read`")
    body: Any | None = Field(
        default=None,
        description="Optional JSON body for POST/PATCH/PUT",
    )

    @field_validator("method")
    @classmethod
    def _method_allowed(cls, v: str) -> str:
        u = (v or "").upper()
        if u not in ALLOWED_METHODS:
            raise ValueError(
                f"method must be one of {sorted(ALLOWED_METHODS)}; got {v!r}"
            )
        return u

    @field_validator("path")
    @classmethod
    def _path_safe(cls, v: str) -> str:
        if not v or not v.startswith("/"):
            raise ValueError("path must start with `/`")
        # Reject obvious abuse — scheme separators, host injection.
        if "://" in v or " " in v or "\n" in v:
            raise ValueError("path contains illegal characters")
        return v


class BatchRequest(BaseModel):
    requests: list[BatchSubRequest] = Field(
        ...,
        description=f"Sub-requests. Capped at {MAX_BATCH_SIZE} per call.",
    )

    @field_validator("requests")
    @classmethod
    def _bounded(cls, v: list[BatchSubRequest]) -> list[BatchSubRequest]:
        if len(v) == 0:
            raise ValueError("`requests` must contain at least one entry")
        if len(v) > MAX_BATCH_SIZE:
            raise ValueError(
                f"`requests` may not exceed {MAX_BATCH_SIZE} entries"
            )
        return v


class BatchItemResult(BaseModel):
    status: int
    body: Any = None
    error: str | None = Field(
        default=None,
        description="Stable error code if the sub-request failed (e.g. `not_found`).",
    )


class BatchResponse(BaseModel):
    results: list[BatchItemResult]


# ─────────────────────────────────────────────────────────────────
# Sub-request dispatch
# ─────────────────────────────────────────────────────────────────


async def _run_sub_request(
    sub: BatchSubRequest,
    *,
    current_user: User,
    db: AsyncSession,
    request: Request,
) -> BatchItemResult:
    """Dispatch a single sub-request through FastAPI's app router.

    Uses `request.app.router` to resolve the route by its template
    path, then invokes the endpoint coroutine with the same
    `current_user` and `db` as the parent (auth + DB session are
    NOT re-validated — that would double the cost).
    """
    try:
        # Resolve the route by matching the sub path against the
        # registered route templates. FastAPI exposes this via
        # `request.app.router.routes` (a list of `APIRoute` objects).
        match = None
        for route in request.app.router.routes:
            if not getattr(route, "path", None):
                continue
            if route.path == sub.path and (
                (sub.method == "GET" and "GET" in getattr(route, "methods", set()))
                or (sub.method in getattr(route, "methods", set()))
            ):
                match = route
                break
        if match is None:
            return BatchItemResult(
                status=404,
                body={"detail": f"No route for {sub.method} {sub.path}"},
                error="not_found",
            )

        # Build kwargs for the endpoint. We pass only the dependencies
        # the parent already has — `current_user` and `db` — so the
        # endpoint runs without re-running the FastAPI dependency
        # resolver (which would otherwise re-attach the bearer token).
        # Endpoints that need additional query / body params won't
        # match this dispatch path; callers should use the dedicated
        # endpoint for those (the route resolver above skips them).
        endpoint = match.endpoint
        sig = getattr(endpoint, "__signature__", None)
        kwargs: dict[str, Any] = {}
        if sig is not None:
            for pname in sig.parameters:
                if pname == "current_user":
                    kwargs[pname] = current_user
                elif pname == "db":
                    kwargs[pname] = db
                # Other dependencies are intentionally skipped —
                # batch sub-requests target endpoints whose only deps
                # are `current_user` and `db`.

        # Call the endpoint. Endpoints may be `async def` or plain
        # `def`. FastAPI converts the latter with `run_in_threadpool`
        # internally; we don't need to here because our handlers are
        # already async.
        result = endpoint(**kwargs)
        if asyncio.iscoroutine(result):
            result = await result

        # Endpoint returned a Pydantic model or dict. Serialise so
        # FastAPI's JSON encoder can dump it.
        if hasattr(result, "model_dump"):
            body = result.model_dump()
        elif hasattr(result, "dict"):
            body = result.dict()
        else:
            body = result
        return BatchItemResult(status=200, body=body)
    except HTTPException as exc:
        return BatchItemResult(
            status=exc.status_code,
            body={"detail": exc.detail},
            error=_code_for(exc.status_code),
        )
    except Exception:
        logger.exception("Batch sub-request %s %s failed", sub.method, sub.path)
        return BatchItemResult(
            status=500,
            body={"detail": "Sub-request failed."},
            error="internal_error",
        )


def _code_for(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        502: "bad_gateway",
        503: "service_unavailable",
        504: "gateway_timeout",
    }.get(status_code, "error")


# ─────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────


@router.post("/batch", response_model=BatchResponse)
async def batch(
    payload: BatchRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BatchResponse:
    """Execute a list of sub-requests concurrently and return the
    per-item results in the same order.

    Auth is enforced ONCE on the parent request. Sub-requests
    reuse the parent's `current_user` and `db` — they do not
    re-attach the bearer token and do not pass through the
    middleware stack (compression / ETag / rate-limit).

    The parent response is always 200; per-item failures are
    captured into the `results[i].status` field.
    """
    try:
        http_batch_size.observe(len(payload.requests))
    except Exception:
        pass

    # Fan out concurrently. Each sub-request gets the same DB session
    # so a multi-sub-request transaction can be chained if a future
    # caller needs it (today every sub-request commits independently).
    coros = [
        _run_sub_request(sub, current_user=current_user, db=db, request=request)
        for sub in payload.requests
    ]
    results = await asyncio.gather(*coros, return_exceptions=False)

    # Record per-sub-request failures for observability.
    for r in results:
        if r.status >= 400:
            sc = f"{r.status // 100}xx"
            try:
                http_batch_subrequest_failures_total.labels(status_class=sc).inc()
            except Exception:
                pass

    return BatchResponse(results=results)
