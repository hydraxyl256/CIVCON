"""
Structured logging configuration for the CIV-CON backend.

Replaces the global `logging.basicConfig(...)` call in `main.py` with:

- A JSON formatter for production (one log record == one JSON object).
- A human-readable text formatter for local development.
- A `request_id` `ContextVar` so every log line emitted during a request
  carries the same id (which matches the `X-Request-Id` response header).
- An `access_log(...)` helper used by the middleware to emit one
  structured access log per request.

The existing `logging.getLogger("CIVCON")` and `logging.getLogger(...)`
calls across the codebase continue to work unchanged. We only swap
the root handler and add a dedicated `CIVCON.access` logger.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

# ============================================================================
# Request-id contextvar
# ============================================================================


# A request-id is a UUIDv4 string set by `RequestIdMiddleware` for the
# duration of a single HTTP request. The JSON formatter reads it so every
# log line emitted during the request is correlatable.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "civcon_request_id", default=None
)


def set_request_id(request_id: str | None) -> None:
    """Set the current request id. Called by the request-id middleware."""
    request_id_var.set(request_id)


def get_request_id() -> str | None:
    """Return the current request id, or None outside a request scope."""
    return request_id_var.get()


# ============================================================================
# Formatters
# ============================================================================


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON object.

    Fields:
      - ts:        ISO-8601 UTC timestamp
      - level:     log level (e.g. "INFO")
      - logger:    logger name (e.g. "CIVCON.access")
      - msg:       the formatted log message
      - request_id: the active request id, if any
      - exc_info:  serialized exception traceback, if any
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # If the log call passed structured `extra={...}` fields, surface
        # them at the top level.
        for key, value in record.__dict__.items():
            if key in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process",
                "taskName",
            }:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter for local development.

    Adds `[req=<id>]` prefix when a request id is active, so a single
    log line is still correlatable to its request.
    """

    DEFAULT_FMT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self.DEFAULT_FMT)

    def format(self, record: logging.LogRecord) -> str:
        rid = get_request_id()
        if rid and "[req=" not in record.getMessage():
            # Inject a single request-id prefix if the caller didn't add
            # one already. We do this by mutating the message in place
            # because logging.Formatter caches the rendered string only
            # after `format()` returns.
            record.msg = f"[req={rid}] {record.msg}"
            record.args = ()
        return super().format(record)


# ============================================================================
# Configuration entry point
# ============================================================================


def configure_logging(
    level: str = "INFO",
    log_format: str = "text",
    access_logger_name: str = "CIVCON.access",
) -> None:
    """Configure the root logger and the access logger.

    Args:
        level: log level name (e.g. "DEBUG", "INFO", "WARNING").
        log_format: "json" for production, "text" for local dev.
        access_logger_name: name of the access-log logger that the
            middleware uses for per-request lines.
    """
    fmt = (log_format or "text").lower()
    formatter: logging.Formatter = JSONFormatter() if fmt == "json" else TextFormatter()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any handlers installed by `logging.basicConfig` (or by
    # uvicorn's own bootstrap) with our single handler so we have one
    # canonical format on stdout.
    root.handlers = [handler]
    root.setLevel(level.upper())

    # The access logger is a child of the root; it will inherit the
    # formatter via the handler we just installed.
    access_logger = logging.getLogger(access_logger_name)
    access_logger.setLevel(logging.INFO)
    # Avoid double-logging through the root by NOT setting propagate.
    access_logger.propagate = True


def access_log(
    logger: logging.Logger,
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    client_ip: str | None = None,
    user_agent: str | None = None,
    extra: dict | None = None,
) -> None:
    """Emit a single structured access-log line.

    Extra fields are passed through the logging `extra=` mechanism, so
    the JSON formatter surfaces them as top-level fields in the JSON
    log line. The text formatter collapses everything into the
    message.
    """
    payload: dict = {
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": round(duration_ms, 2),
    }
    if client_ip:
        payload["client_ip"] = client_ip
    if user_agent:
        payload["user_agent"] = user_agent
    if extra:
        payload.update(extra)

    # Build a single human-readable message for the text formatter.
    # The JSON formatter pulls the structured fields from `record.__dict__`
    # via the formatter's `extra` pass-through logic.
    msg = (
        f"method={method} path={path} status={status} "
        f"duration_ms={payload['duration_ms']}"
    )
    if client_ip:
        msg += f" client_ip={client_ip}"

    logger.info(msg, extra=payload)
