"""
Sentry initialisation for the CIV-CON backend.

The `init_sentry()` helper is called once at module load (from
`app/main.py`). When `SENTRY_DSN` is empty (the default in dev) the
function is a no-op, so the absence of a Sentry project does not
impose any overhead on local development.

The Sentry SDK is imported lazily inside the function — that way
a broken `sentry-sdk` install does not prevent the app from
booting. Any failure inside `init_sentry()` is caught and logged
so a transient Sentry outage can never take the API down.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger("CIVCON.observability")


def init_sentry() -> None:
    """Initialise the Sentry SDK if a DSN is configured.

    Default behaviour (no env vars set) is a no-op so the app
    starts up the same way it always has. Set `SENTRY_DSN` to enable
    error tracking, and optionally `SENTRY_TRACES_SAMPLE_RATE` /
    `SENTRY_PROFILES_SAMPLE_RATE` to capture performance data.

    A failure to initialise Sentry (e.g. SDK import error, network
    issue) is logged at WARNING and the function returns without
    raising — observability must never be a single point of failure
    for the API.
    """
    if not settings.sentry_dsn:
        logger.info("Sentry DSN not set — error tracking disabled")
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except Exception as exc:
        logger.warning(
            "Sentry SDK could not be imported — error tracking disabled: %s",
            exc,
        )
        return

    environment = settings.sentry_environment or settings.environment

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=environment,
            release=settings.app_version,
            traces_sample_rate=float(settings.sentry_traces_sample_rate),
            profiles_sample_rate=float(settings.sentry_profiles_sample_rate),
            send_default_pii=bool(settings.sentry_send_default_pii),
            integrations=[
                FastApiIntegration(),
                StarletteIntegration(),
                SqlalchemyIntegration(),
                # Capture ERROR-level log records as Sentry events, and
                # tag INFO-level records as breadcrumbs. This piggy-backs
                # on the existing structured-logging setup so the
                # Sentry timeline matches the log stream.
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        logger.info(
            "Sentry initialised (env=%s, release=%s, traces_sample_rate=%.2f)",
            environment,
            settings.app_version,
            float(settings.sentry_traces_sample_rate),
        )
    except Exception as exc:
        logger.warning("Sentry init failed — error tracking disabled: %s", exc)


def set_request_id_tag(request_id: str | None) -> None:
    """Attach the current request id to Sentry as a tag.

    Best-effort: if the Sentry SDK is not installed (or has not
    been initialised because no DSN is configured) the function is
    a silent no-op. This lets `RequestIdMiddleware` call it
    unconditionally without a guard.
    """
    if not request_id:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", request_id)
    except Exception:
        # Either the SDK is missing or Sentry is not initialised.
        # Either way, this is best-effort — never raise from a
        # logging helper.
        pass
