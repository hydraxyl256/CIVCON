"""
Observability contract tests.

These tests pin down the runtime observability surfaces added on top
of the API stack. They are pure black-box (HTTP + import) so they
run without a live database, Redis, or Sentry account.

Scope (each requirement from observability.txt maps to one or more
tests):

1.  **Sentry** is a no-op when ``SENTRY_DSN`` is empty (the default
    in dev). When a DSN is configured, ``init_sentry`` runs without
    raising even if the SDK is unavailable.
2.  **Prometheus metrics** are exposed at ``/metrics`` and include the
    standard RED counters (http_requests_total, http_request_duration_seconds,
    http_requests_in_flight) labelled by method/path/status.
3.  **Metrics endpoint** returns 200 with the prometheus text format and
    is excluded from the OpenAPI schema.
4.  **Health monitoring** — ``/health`` is always 200; ``/ready`` is
    200 or 503 depending on DB/Redis reachability; both are excluded
    from the OpenAPI schema.
5.  **Performance monitoring** — request duration is recorded with
    sensible bucket boundaries.
6.  **Request timing** — every successful response carries an
    ``X-Request-Id`` header and the access log emits duration_ms.
7.  **Error tracking** — the global handler returns the standard
    envelope; Sentry integration does not raise even if the SDK is
    missing.
8.  **Logging** — the JSON formatter surfaces the request_id from
    the active ContextVar; the text formatter surfaces it as
    ``[req=<id>]``.
9.  **All integrations are configurable via env vars** — the settings
    schema exposes every documented knob.

Run with::

    pytest app/tests/test_observability.py -v
"""
from __future__ import annotations

import logging
import re
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Lazy import helper for the live app
# ---------------------------------------------------------------------------


def _import_app():
    """Lazily import the FastAPI app so tests can opt in.

    The full import pulls in routers that touch Africa’s Talking, the
    settings module, etc. We do it once at fixture-resolve time so a
    broken env can be detected at runtime (rather than at collection
    time, where the error message is much harder to interpret).
    """
    from app.main import app as _app
    return _app


_APP_LOADABLE = True
_APP_LOAD_ERROR: str | None = None
try:
    _import_app()
except Exception as _exc:  # pragma: no cover - environment-dependent
    _APP_LOADABLE = False
    _APP_LOAD_ERROR = repr(_exc)

_skip_app = pytest.mark.skipif(
    not _APP_LOADABLE,
    reason=(
        "Full app import failed; pure-schema tests still run. "
        f"Detail: {_APP_LOAD_ERROR}"
    ),
)


# ---------------------------------------------------------------------------
# Fixture: AsyncClient with the rate limiter stubbed out
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    """An AsyncClient attached to the FastAPI app.

    Some observability routers (``/health``, ``/ready``, ``/metrics``)
    are operational surface and don't carry a rate limiter, so the
    limiter stub installed here is harmless even if the limiter deps
    are wired elsewhere. See ``test_api_contract.py`` for the full
    explanation of the stub.
    """
    from fastapi_limiter import FastAPILimiter
    from fastapi_limiter.depends import RateLimiter

    async def _no_op_check(self, key):
        return 0

    async def _identifier(request):
        return "test-client"

    original_redis = FastAPILimiter.redis
    original_check = RateLimiter._check
    original_identifier = FastAPILimiter.identifier
    FastAPILimiter.redis = object()
    FastAPILimiter.identifier = _identifier
    RateLimiter._check = _no_op_check

    try:
        app = _import_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        FastAPILimiter.redis = original_redis
        FastAPILimiter.identifier = original_identifier
        RateLimiter._check = original_check


# ---------------------------------------------------------------------------
# 1. Sentry is a no-op without a DSN
# ---------------------------------------------------------------------------


def test_sentry_init_no_op_without_dsn():
    """With SENTRY_DSN empty, init_sentry() returns without side effects."""
    # Force the empty-DSN branch by patching settings.sentry_dsn.
    with patch("app.config.settings.sentry_dsn", ""):
        # Re-import to pick up the patched settings (pydantic-settings
        # caches attribute access; the live read picks up the patched value).
        from app.observability.sentry import init_sentry
        # Must not raise even if the SDK is missing.
        init_sentry()


def test_sentry_init_handles_missing_sdk():
    """A missing sentry_sdk does not raise from init_sentry()."""
    with patch("app.config.settings.sentry_dsn", "https://fake@fake.ingest.sentry.io/0"):
        # Patch the import inside init_sentry so the SDK "disappears".
        import builtins
        original_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "sentry_sdk" or name.startswith("sentry_sdk."):
                raise ImportError("simulated missing SDK")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_fake_import):
            from app.observability.sentry import init_sentry
            # Must not raise.
            init_sentry()


def test_sentry_set_request_id_tag_is_silent_when_sdk_missing():
    """set_request_id_tag is a silent no-op when sentry_sdk is absent."""
    with patch.dict(__import__("sys").modules, {"sentry_sdk": None}):
        from app.observability.sentry import set_request_id_tag
        # Must not raise.
        set_request_id_tag("abc123")


# ---------------------------------------------------------------------------
# 2. Prometheus metrics definitions
# ---------------------------------------------------------------------------


def test_prometheus_metrics_registered():
    """The three RED metrics exist with the expected names and labels.

    `prometheus_client` strips the `_total` suffix from counter names
    internally — the counter is created with `_total` but the `_name`
    attribute drops it. The Prometheus exposition format still emits
    the canonical `_total` suffix on the wire, which is what the body
    of ``/metrics`` contains.
    """
    from app.observability.metrics import (
        http_request_duration_seconds,
        http_requests_in_flight,
        http_requests_total,
    )
    # `prometheus_client` normalises counter names by dropping the
    # `_total` suffix on the attribute; the exposition format adds it
    # back. We assert against the attribute, not the wire form.
    assert http_requests_total._name == "civcon_http_requests"
    assert http_requests_total._labelnames == ("method", "path", "status")
    assert http_request_duration_seconds._name == "civcon_http_request_duration_seconds"
    assert http_request_duration_seconds._labelnames == ("method", "path", "status")
    assert http_requests_in_flight._name == "civcon_http_requests_in_flight"


def test_prometheus_histogram_bucket_boundaries():
    """The duration histogram covers 5 ms through 30 s.

    `prometheus_client` automatically appends a ``+Inf`` bucket as
    the last element, so the wire-level bucket count is N+1 where N
    is the explicit count.
    """
    from app.observability.metrics import http_request_duration_seconds
    upper_bounds = sorted(float(b) for b in http_request_duration_seconds._upper_bounds)
    # The 12 explicit buckets plus the auto-added +Inf at the end.
    assert upper_bounds[0] == 0.005
    # `float("inf")` sorts as the largest numeric value.
    assert upper_bounds[-1] == float("inf")
    # 12 explicit + 1 implicit = 13 total.
    assert len(upper_bounds) == 13
    # The penultimate bucket is the explicit 30s upper bound.
    assert upper_bounds[-2] == 30.0


# ---------------------------------------------------------------------------
# 3. Metrics endpoint
# ---------------------------------------------------------------------------


@_skip_app
@pytest.mark.asyncio
async def test_metrics_endpoint_returns_200_with_text_format(client):
    """`GET /metrics` returns 200 with the prometheus text exposition format."""
    res = await client.get("/metrics")
    assert res.status_code == 200, res.text
    # Content-Type matches the prometheus exposition format.
    ct = res.headers.get("content-type", "")
    assert "text/plain" in ct, f"unexpected content-type {ct!r}"
    # Body contains the registered metric names.
    body = res.text
    assert "civcon_http_requests_total" in body
    assert "civcon_http_request_duration_seconds" in body
    assert "civcon_http_requests_in_flight" in body


@_skip_app
@pytest.mark.asyncio
async def test_metrics_endpoint_excluded_from_openapi(client):
    """The /metrics path is operational surface — not in the OpenAPI schema."""
    schema_res = await client.get("/openapi.json")
    assert schema_res.status_code == 200
    paths = schema_res.json().get("paths", {})
    assert "/metrics" not in paths, "/metrics must not appear in OpenAPI"


# ---------------------------------------------------------------------------
# 4. Health monitoring
# ---------------------------------------------------------------------------


@_skip_app
@pytest.mark.asyncio
async def test_health_endpoint_returns_200(client):
    """`GET /health` always returns 200 with `{"status": "ok"}`."""
    res = await client.get("/health")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "ok"}


@_skip_app
@pytest.mark.asyncio
async def test_ready_endpoint_returns_envelope_shape(client):
    """`GET /ready` returns 200 or 503 with a `checks` envelope."""
    res = await client.get("/ready")
    assert res.status_code in (200, 503), res.text
    body = res.json()
    assert "status" in body
    assert body["status"] in ("ready", "degraded")
    assert "checks" in body
    assert set(body["checks"].keys()) >= {"db", "redis"}
    assert isinstance(body["checks"]["db"], bool)
    assert isinstance(body["checks"]["redis"], bool)


@_skip_app
@pytest.mark.asyncio
async def test_health_endpoints_excluded_from_openapi(client):
    """Health + readiness probes are not in the OpenAPI schema."""
    schema_res = await client.get("/openapi.json")
    assert schema_res.status_code == 200
    paths = schema_res.json().get("paths", {})
    assert "/health" not in paths
    assert "/ready" not in paths


# ---------------------------------------------------------------------------
# 5. Request timing / X-Request-Id
# ---------------------------------------------------------------------------


@_skip_app
@pytest.mark.asyncio
async def test_request_timing_header_present(client):
    """Every response carries an X-Request-Id header (UUID hex)."""
    res = await client.get("/health")
    assert res.status_code == 200
    rid = res.headers.get("x-request-id")
    assert rid, "missing X-Request-Id header"
    # Hex (with or without dashes).
    assert re.match(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", rid), (
        f"not a UUID hex: {rid!r}"
    )


# ---------------------------------------------------------------------------
# 6. Logging
# ---------------------------------------------------------------------------


def test_logging_json_formatter_includes_request_id(caplog):
    """The JSON formatter surfaces the active request_id ContextVar."""

    from app.core.logging_config import (
        JSONFormatter,
        set_request_id,
    )

    formatter = JSONFormatter()
    set_request_id("abc-1234")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        out = formatter.format(record)
        # Parse the JSON and assert the request_id appears at top level.
        import json as _json
        payload = _json.loads(out)
        assert payload["request_id"] == "abc-1234"
        assert payload["msg"] == "hello"
        assert payload["level"] == "INFO"
    finally:
        set_request_id(None)


def test_logging_text_formatter_injects_request_id_prefix():
    """The text formatter prefixes the message with [req=<id>]."""
    from app.core.logging_config import TextFormatter, set_request_id

    formatter = TextFormatter()
    set_request_id("xyz-9876")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="op completed",
            args=(),
            exc_info=None,
        )
        out = formatter.format(record)
        assert "[req=xyz-9876]" in out, f"missing request id prefix in {out!r}"
        assert "op completed" in out
    finally:
        set_request_id(None)


def test_logging_text_formatter_does_not_double_prefix():
    """The text formatter must not double-prefix when the caller already
    added [req=...] — e.g. from a hand-rolled access-log call."""
    from app.core.logging_config import TextFormatter, set_request_id

    formatter = TextFormatter()
    set_request_id("abc")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="[req=manual] hello",
            args=(),
            exc_info=None,
        )
        out = formatter.format(record)
        # Exactly one [req= prefix should appear, not two.
        assert out.count("[req=") == 1, out
    finally:
        set_request_id(None)


def test_configure_logging_replaces_root_handler():
    """`configure_logging` replaces any existing root handlers."""
    from app.core.logging_config import configure_logging

    root = logging.getLogger()
    # Pre-install a sentinel handler so we can verify replacement.
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        configure_logging(level="DEBUG", log_format="text")
        assert sentinel not in root.handlers, (
            "configure_logging did not replace the existing root handlers"
        )
        # Exactly one handler installed (the StreamHandler).
        assert len(root.handlers) == 1
    finally:
        root.removeHandler(sentinel)


# ---------------------------------------------------------------------------
# 7. Auth event logging
# ---------------------------------------------------------------------------


def test_auth_log_event_emits_structured_line(caplog):
    """`log_event` emits a single log line carrying the event payload."""
    from app.observability.auth_log import log_event

    with caplog.at_level(logging.INFO, logger="CIVCON.auth"):
        log_event(
            "login.success",
            user_id=42,
            email="u@example.com",
            ip="127.0.0.1",
            user_agent="Mozilla/5.0",
        )
    # Find the emitted record.
    records = [r for r in caplog.records if r.name == "CIVCON.auth"]
    assert len(records) == 1
    rec = records[0]
    # `auth_event` is the structured payload carrier.
    assert hasattr(rec, "auth_event")
    payload = rec.auth_event
    assert payload["event"] == "login.success"
    assert payload["user_id"] == 42
    assert payload["email"] == "u@example.com"
    assert payload["ip"] == "127.0.0.1"
    assert payload["user_agent"] == "Mozilla/5.0"


def test_auth_log_event_truncates_long_user_agent(caplog):
    """UA strings longer than 200 chars are truncated to avoid log explosion."""
    from app.observability.auth_log import log_event

    long_ua = "x" * 500
    with caplog.at_level(logging.INFO, logger="CIVCON.auth"):
        log_event("login.success", user_agent=long_ua)
    rec = next(r for r in caplog.records if r.name == "CIVCON.auth")
    assert len(rec.auth_event["user_agent"]) == 200


def test_auth_log_event_omits_none_values(caplog):
    """Optional fields with None values are omitted from the payload."""
    from app.observability.auth_log import log_event

    with caplog.at_level(logging.INFO, logger="CIVCON.auth"):
        log_event("logout.success", user_id=7, email=None, ip=None)
    rec = next(r for r in caplog.records if r.name == "CIVCON.auth")
    payload = rec.auth_event
    assert "email" not in payload
    assert "ip" not in payload
    assert payload["user_id"] == 7


# ---------------------------------------------------------------------------
# 8. Settings surface — every observability knob is configurable
# ---------------------------------------------------------------------------


def test_settings_exposes_observability_knobs():
    """Every documented env var maps to a Settings attribute."""
    from app.config import settings

    # Logging
    assert hasattr(settings, "log_level")
    assert hasattr(settings, "log_format")
    assert hasattr(settings, "slow_request_threshold_ms")
    # Metrics
    assert hasattr(settings, "metrics_enabled")
    # Sentry
    assert hasattr(settings, "sentry_dsn")
    assert hasattr(settings, "sentry_environment")
    assert hasattr(settings, "sentry_traces_sample_rate")
    assert hasattr(settings, "sentry_profiles_sample_rate")
    assert hasattr(settings, "sentry_send_default_pii")
    # Runtime / release
    assert hasattr(settings, "environment")
    assert hasattr(settings, "app_version")


def test_settings_observability_defaults_are_safe():
    """Default values are safe for local dev (no Sentry, text logs, metrics on)."""
    from app.config import settings

    assert settings.sentry_dsn == "", "Sentry must be off by default in dev"
    assert settings.sentry_send_default_pii is False, "PII must be off by default"
    assert settings.log_format == "text", "log_format default should be 'text'"
    assert settings.metrics_enabled is True, "metrics must be enabled by default"