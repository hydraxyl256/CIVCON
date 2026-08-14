"""
Web Push router.

Endpoints:
  GET    /push/vapid-public-key     → returns the VAPID public key.
  POST   /push/subscribe            → register/update a push endpoint.
  DELETE /push/subscribe            → remove a push endpoint.
  POST   /push/test                 → send a hello payload to the current
                                      user's endpoints (debug / smoke).

The VAPID public-key endpoint is intentionally unauthenticated so that
the SPA can fetch it BEFORE the user signs in (the `beforeinstallprompt`
→ notification-permission flow can happen on a guest session). The
endpoint itself is just a base64url string with no PII, so leaving it
public is safe.

`POST /push/subscribe` is idempotent: an `endpoint` is unique in the DB
(`uq_push_subscriptions_endpoint`). If the same browser re-subscribes
(rotation, same device, OS reinstall) we UPDATE the keys + UA rather
than creating a duplicate row. This avoids the "20 ghost subscriptions
per user" failure mode.

`DELETE /push/subscribe` accepts the endpoint in the body and removes
any matching row belonging to the caller. We don't 404 if the row
isn't there — that's a normal race during sign-out.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PushSubscription, Role, User
from app.routers.permissions import require_role
from app.schemas import (
    PushSubscriptionIn,
    PushSubscriptionOut,
    PushTestResult,
    VapidPublicKey,
)
from app.services import push_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push", tags=["Push Notifications"])


@router.get(
    "/vapid-public-key",
    response_model=VapidPublicKey,
    summary="Return the VAPID public key (base64url).",
)
async def get_vapid_public_key() -> VapidPublicKey:
    try:
        key = push_service.public_key()
    except RuntimeError as exc:
        # We surface a 503 rather than 500 — the operator can fix this
        # by setting the env var; it's a configuration issue, not a bug.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return VapidPublicKey(key=key)


@router.post(
    "/subscribe",
    response_model=PushSubscriptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register or refresh a push subscription for the caller.",
)
async def subscribe(
    body: PushSubscriptionIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_role([Role.CITIZEN, Role.MP, Role.JOURNALIST, Role.ADMIN])
    ),
) -> PushSubscriptionOut:
    ua = current_user_agent_from_state()  # see helper below
    try:
        existing = (
            await db.execute(
                select(PushSubscription).where(
                    PushSubscription.endpoint == body.endpoint
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            sub = PushSubscription(
                user_id=current_user.id,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
                user_agent=ua,
            )
            db.add(sub)
            await db.commit()
            await db.refresh(sub)
            return PushSubscriptionOut.model_validate(sub)

        # Same endpoint re-subscribing — refresh its keys and (if it
        # changed hands) the owning user. We never let one user's
        # endpoint be silently reassigned to another; require_role
        # already gated this call to an authenticated user, so
        # `existing.user_id == current_user.id` is the normal case.
        if existing.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This device is already registered to another account.",
            )
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
        existing.user_agent = ua
        await db.commit()
        await db.refresh(existing)
        return PushSubscriptionOut.model_validate(existing)
    except IntegrityError as exc:
        # Race: another request inserted the same endpoint between our
        # SELECT and our INSERT. Treat as success by re-reading.
        await db.rollback()
        existing = (
            await db.execute(
                select(PushSubscription).where(
                    PushSubscription.endpoint == body.endpoint
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to register subscription.",
            ) from exc
        return PushSubscriptionOut.model_validate(existing)


@router.delete(
    "/subscribe",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a push subscription (called on permission revoke).",
)
async def unsubscribe(
    body: PushSubscriptionIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_role([Role.CITIZEN, Role.MP, Role.JOURNALIST, Role.ADMIN])
    ),
) -> None:
    """Best-effort delete — 204 even if the row is already gone. This
    avoids a noisy UX on logout where the user toggles push off, then
    off again, then closes the tab."""
    existing = (
        await db.execute(
            select(PushSubscription).where(
                PushSubscription.endpoint == body.endpoint,
                PushSubscription.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.commit()


@router.post(
    "/test",
    response_model=PushTestResult,
    summary="Send a hello payload to the caller's subscriptions (debug).",
)
async def push_test(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_role([Role.CITIZEN, Role.MP, Role.JOURNALIST, Role.ADMIN])
    ),
) -> PushTestResult:
    """Used by the Settings UI to confirm push is wired up end-to-end.
    Sends a benign "CIV-CON notifications are working" payload."""
    results = await push_service.send_to_user(
        db,
        current_user.id,
        {
            "title": "CIV-CON",
            "body": "Notifications are working. You're all set.",
            "url": "/notifications",
            "tag": "push-test",
        },
        ttl=60,
    )
    return PushTestResult(**push_service.summarise(results))


# ---------------------------------------------------------------------------
# User-Agent capture
# ---------------------------------------------------------------------------
# FastAPI's `Request` is injected as a function arg, but our handlers
# don't take one. Rather than thread the `Request` through every handler
# we use a tiny module-level indirection: handlers that want the UA
# call `current_user_agent_from_state()`. We use a ContextVar so that
# concurrent requests can't bleed into each other.

from contextvars import ContextVar

_user_agent_var: ContextVar[str | None] = ContextVar("_user_agent_var", default=None)


def set_current_user_agent(ua: str | None) -> None:
    _user_agent_var.set(ua)


def current_user_agent_from_state() -> str | None:
    # Truncate to fit the column — VARCHAR(255). Browsers typically
    # return UA strings of ~120 chars; mobile Safari can be longer.
    ua = _user_agent_var.get()
    if ua is None:
        return None
    return ua[:255]