import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ----- Database -----
    database_hostname: str
    database_port: str
    database_password: str
    database_name: str
    database_username: str

    # ----- Database connection pool -----
    # Defaults are tuned for a single Render worker running FastAPI +
    # asyncpg. Override per environment.
    #
    #   pool_size       — persistent connections kept open (default 10)
    #   max_overflow    — additional connections allowed beyond pool_size
    #                     under load (default 20, total ceiling 30)
    #   pool_recycle    — seconds before a connection is recycled; must be
    #                     below any cloud LB / pgbouncer idle timeout.
    #                     1800s (30 min) is a safe default.
    #   pool_timeout    — seconds to wait for a free connection before
    #                     raising QueuePoolTimeout (default 30s)
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800
    db_pool_timeout: int = 30

    # ----- API runtime -----
    # Per-request processing budget. Requests that exceed this are
    # answered with a 504 by RequestTimeoutMiddleware. WebSockets are
    # exempt because they're long-lived by design.
    request_timeout_seconds: float = 30.0
    # Maximum allowed request body size, in bytes. Requests with a
    # Content-Length above this are rejected with a 413 by
    # RequestSizeLimitMiddleware. Default 10 MiB.
    max_request_body_bytes: int = 10 * 1024 * 1024
    # Logging format: "json" for production, "text" for local dev.
    log_format: str = "text"
    log_level: str = "INFO"
    # Threshold for the slow-request warning emitted by
    # AccessLogMiddleware. A request that takes longer than this (in
    # ms) gets a separate WARNING log line in addition to the normal
    # access log.
    slow_request_threshold_ms: float = 1000.0
    # Toggle for the Prometheus /metrics endpoint. When False the
    # endpoint is not registered (handy for tests).
    metrics_enabled: bool = True

    # ----- Runtime environment -----
    # Single source of truth for the runtime environment. Surfaced in
    # Sentry events, /health responses, and access logs.
    environment: str = "development"
    # App version surfaced to Sentry and the root endpoint.
    app_version: str = "1.0.0"

    # ----- Sentry (backend) -----
    # DSN is the Sentry project ingest URL. Leave empty to disable
    # error tracking entirely (the default for local dev).
    sentry_dsn: str = ""
    # Override the Sentry environment (otherwise falls back to
    # `settings.environment`).
    sentry_environment: str = ""
    # Fraction of transactions to capture as performance traces.
    # 0.0 = no tracing, 1.0 = trace everything. 0.1 is a common
    # production setting.
    sentry_traces_sample_rate: float = 0.0
    # Fraction of transactions to profile. Same convention as
    # traces_sample_rate. Profiling requires a Sentry plan that
    # supports it.
    sentry_profiles_sample_rate: float = 0.0
    # Whether to include PII (IP addresses, user identifiers) in
    # Sentry events. Default False — we do not want PII by default.
    sentry_send_default_pii: bool = False

    # ----- Security / JWT -----
    secret_key: str
    session_secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # ----- OAuth -----
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    linkedin_client_id: str
    linkedin_client_secret: str
    linkedin_redirect_uri: str = "http://localhost:8000/auth/linkedin/callback"

    # ----- Africa's Talking (USSD + SMS) -----
    africastalking_username: str
    africastalking_api_key: str
    live_username: str = "sandbox"
    live_api_key: str = ""
    default_civic_office_number: str = os.getenv("DEFAULT_CIVIC_OFFICE_NUMBER", "+256700000000")

    # ----- Email -----
    mail_username: str
    mail_password: str
    mail_from: str
    mail_port: int = 587
    mail_server: str = "smtp.fastmail.com"
    mail_tls: bool = True
    mail_ssl: bool = False

    # ----- Redis -----
    redis_url: str

    # ----- URLs -----
    frontend_url: str = "http://localhost:5173"
    backend_url: str = "http://localhost:8000"

    # ----- Cloudinary -----
    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_cloudinary_api_secret: str | None = None
    cloudinary_api_secret: str

    # ----- Resend -----
    resend_api_key: str
    sender_email: str

    # ----- Fallbacks -----
    fallback_phone: str = "+256700000000"
    fallback_mp_id: int = 1

    # ----- VAPID (Web Push) -----
    # The PRIVATE key never leaves the server. The PUBLIC key is served
    # to clients so they can encrypt payloads to the server's VAPID
    # identifier. Both must be base64url-encoded (no padding) DER keys
    # exactly as `py-vapid` produces them.
    #
    # Generate a fresh pair once with:
    #     python -c "from py_vapid import Vapid; v=Vapid(); v.generate_keys(); print(v.export_public_key().decode()); print(v.export_private_key().decode())"
    #
    # `vapid_subject` is the contact URL (mailto:...) or mailto: address
    # the push service may use to contact the operator. Most providers
    # require this; default to a contact mailto.
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = "mailto:admin@civcon.app"

    # ----- Env plumbing -----
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        """
        Build the async PostgreSQL connection URL dynamically.

        SSL behaviour:
          - If the host is localhost / 127.0.0.1, we omit sslmode (plain TCP).
          - For any other host we force sslmode=require so certificates are
            validated by asyncpg against the system trust store. NEVER bypass
            certificate verification in production.
        """
        host = (self.database_hostname or "").lower().strip()
        use_ssl = host not in {"localhost", "127.0.0.1", ""}
        query = "?sslmode=require" if use_ssl else ""
        return (
            f"postgresql+asyncpg://{self.database_username}:"
            f"{self.database_password}@{self.database_hostname}:"
            f"{self.database_port}/{self.database_name}{query}"
        )


settings = Settings()
