"""
Standard response and error envelope shapes for the CIV-CON API.

The frontend's compatibility boundary is preserved by ALWAYS populating
the `detail` field of `ErrorResponse` with a non-empty human-readable
string — the existing `err.response.data.detail` read path keeps working
unchanged. New code can branch on `code` (machine-readable) and surface
field-level errors via `errors[]`.
"""
from __future__ import annotations

import math
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# Error envelope
# ============================================================================


class ErrorDetail(BaseModel):
    """One field-level validation error."""

    field: str = Field(..., description="Dotted path of the offending field, e.g. 'body.password'")
    message: str = Field(..., description="Human-readable error message")
    code: str | None = Field(default=None, description="Stable error code, e.g. 'string_too_short'")


class ErrorResponse(BaseModel):
    """Standard error envelope returned for every non-2xx response.

    Compatibility contract with the existing frontend:

    - `detail` is ALWAYS a non-empty human-readable string. The existing
      frontend reads `err.response.data.detail` (see
      `src/pages/Settings.tsx`, `src/pages/Profile.tsx`, etc.) — that
      path keeps resolving to a real string with this envelope.

    - `code` is a stable machine-readable error code. The frontend may
      branch on it (e.g. show a different toast for `validation_error`).

    - `request_id` is the per-request UUID echoed in the `X-Request-Id`
      response header. The frontend may surface this in support tickets.

    - `errors` is a list of field-level errors for 422 validation
      responses. The frontend can highlight the offending form fields.

    - `hint` is an optional remediation hint (e.g. "Try a longer
      password" for a 422).
    """

    detail: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)
    errors: list[ErrorDetail] = Field(default_factory=list)
    hint: str | None = None

    model_config = ConfigDict(extra="ignore")


# ----------------------------------------------------------------------------
# Code derivation from HTTP status
# ----------------------------------------------------------------------------


_STATUS_TO_CODE: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    408: "request_timeout",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


def code_for_status(status_code: int) -> str:
    """Return the canonical machine-readable code for an HTTP status."""
    return _STATUS_TO_CODE.get(status_code, "error")


# ============================================================================
# Pagination envelope (opt-in)
# ============================================================================

T = TypeVar("T")


class PageMeta(BaseModel):
    """Pagination metadata for the opt-in `Page[T]` envelope."""

    page: int = Field(..., ge=1)
    size: int = Field(..., ge=1)
    total: int = Field(..., ge=0)
    pages: int = Field(..., ge=0)

    model_config = ConfigDict(extra="ignore")


class Page(BaseModel, Generic[T]):
    """Opt-in paginated envelope.

    Existing endpoints keep returning raw lists (`List[X]`) so the
    frontend's existing data-shape contract is preserved. New endpoints
    (or migrations) can return `Page[X](items=..., meta=PageMeta(...))`
    to opt into the consistent envelope.

    Example:
        @router.get("/items", response_model=Page[ItemOut])
        async def list_items(page: int = 1, size: int = 20, db = Depends(get_db)):
            items, total = await fetch_items(db, page, size)
            return paginate(items, total=total, page=page, size=size)
    """

    items: list[T] = Field(default_factory=list)
    meta: PageMeta

    model_config = ConfigDict(extra="ignore")


def paginate(items: list[Any], total: int, page: int, size: int) -> dict:
    """Build a `Page[dict]` payload for direct return from a router.

    Returns a plain dict so Pydantic generics don't have to be reified
    at runtime — the router's `response_model=Page[ItemOut]` does the
    validation.
    """
    pages = math.ceil(total / size) if size > 0 else 0
    return {
        "items": items,
        "meta": {
            "page": page,
            "size": size,
            "total": total,
            "pages": pages,
        },
    }
