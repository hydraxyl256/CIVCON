"""
Web Push service.

Thin wrapper around `pywebpush` that:
  - loads VAPID credentials from settings (no key material in code),
  - encrypts payloads to per-subscription ECDH keys (handled by
    pywebpush),
  - fans out a single payload to all of a user's active subscriptions,
  - prunes dead subscriptions (410 Gone / 404 Not Found responses),
  - records `last_used_at` on successful deliveries so we can age out
    inactive endpoints.

Design notes:
  - The fan-out is sequential: it does NOT use a background queue. Web
    Push volume per user is small (1–5 endpoints). If we ever cross
    ~100 users × 5 endpoints per push, swap to a Celery task.
  - All pywebpush errors are caught and translated to a `PushResult`
    tuple so the router can return a tidy summary without the caller
    having to know about HTTPError.
  - We never log full payload contents — push bodies may contain user
    names / message excerpts / URLs, which are PII-adjacent.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PushSubscription

logger = logging.getLogger(__name__)


@dataclass
class PushResult:
    """Outcome of a single push delivery attempt."""

    endpoint: str
    ok: bool
    status: int | None = None
    error: str | None = None
    pruned: bool = False  # True if the subscriber was removed (410 Gone)


def _vapid_private_key() -> str:
    pk = settings.vapid_private_key
    if not pk:
        raise RuntimeError(
            "VAPID private key is not configured. Set VAPID_PRIVATE_KEY in the "
            "backend environment before sending push notifications."
        )
    return pk


def _vapid_claims() -> dict:
    return {"sub": settings.vapid_subject}


def public_key() -> str:
    """The VAPID public key, base64url-encoded. Safe to expose to clients."""
    pk = settings.vapid_public_key
    if not pk:
        raise RuntimeError(
            "VAPID public key is not configured. Set VAPID_PUBLIC_KEY in the "
            "backend environment."
        )
    return pk


def _send_one(
    subscription: PushSubscription,
    payload_json: str,
    ttl: int,
) -> PushResult:
    """Send one push. Synchronous because pywebpush is sync; we keep
    it that way and let the router run it via `asyncio.to_thread`."""
    # Imported lazily so that the module can be imported during tests
    # that don't have pywebpush installed (and so the rest of the app
    # doesn't pay the import cost on every request).
    from pywebpush import webpush
    from pywebpush.exceptions import WebPushException

    sub_info = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }

    try:
        webpush(
            sub_info,
            payload_json,
            vapid_private_key=_vapid_private_key(),
            vapid_claims=_vapid_claims(),
            ttl=ttl,
        )
        return PushResult(endpoint=subscription.endpoint, ok=True)
    except WebPushException as exc:
        # The push service returns 404/410 when the endpoint is gone.
        # Anything else is a transient failure worth retrying later.
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
        is_dead = status in (404, 410)
        logger.warning(
            "push delivery failed endpoint=%s status=%s dead=%s",
            subscription.endpoint[:80],
            status,
            is_dead,
        )
        return PushResult(
            endpoint=subscription.endpoint,
            ok=False,
            status=status,
            error=str(exc),
            pruned=is_dead,
        )
    except Exception as exc:
        logger.exception("unexpected push error: %s", exc)
        return PushResult(endpoint=subscription.endpoint, ok=False, error=str(exc))


async def send_to_user(
    db: AsyncSession,
    user_id: int,
    payload: dict,
    ttl: int = 60 * 60 * 24,
) -> list[PushResult]:
    """Fan out a JSON payload to every active subscription belonging
    to `user_id`. Prunes any subscription whose endpoint is gone.

    Args:
        db: an active async session.
        user_id: the recipient user.
        payload: dict serialised to JSON and sent as the push body.
        ttl: how long the push service may hold the message if the
            device is offline. Default 24h; aligned with the SW
            background-sync queue's retention.

    Returns:
        A list of `PushResult`, one per subscription that was tried.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    rows = await db.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    subs: list[PushSubscription] = list(rows.scalars().all())
    if not subs:
        return []

    payload_json = json.dumps(payload, separators=(",", ":"))

    import asyncio

    results = await asyncio.gather(
        *(asyncio.to_thread(_send_one, s, payload_json, ttl) for s in subs),
        return_exceptions=False,
    )

    # Bump `last_used_at` on successful deliveries and prune dead ones.
    now = datetime.now(UTC)
    dead_endpoints: list[str] = []
    for sub, result in zip(subs, results, strict=False):
        if result.ok:
            sub.last_used_at = now
        elif result.pruned:
            dead_endpoints.append(sub.endpoint)

    if dead_endpoints:
        await db.execute(
            PushSubscription.__table__.delete().where(
                PushSubscription.endpoint.in_(dead_endpoints)
            )
        )
        logger.info("pruned %d dead push subscriptions", len(dead_endpoints))

    await db.commit()
    return results


def summarise(results: Iterable[PushResult]) -> dict:
    """Compact summary for the `/push/test` debug endpoint."""
    sent = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    pruned = sum(1 for r in results if r.pruned)
    return {"sent": sent, "failed": failed, "pruned": pruned, "total": sent + failed}