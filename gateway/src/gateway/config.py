"""Gateway configuration loaded from environment variables.

All settings live under the ``GATEWAY_`` prefix. ``Settings`` is constructed
once at startup (see ``main.py``) and injected into the FastAPI app state.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level configuration.

    Read from environment variables with ``GATEWAY_`` prefix; ``.env`` files
    are loaded automatically when present.
    """

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = Field(default="0.0.0.0", description="Bind address")
    port: int = Field(default=8080, ge=1, le=65535, description="Bind port")
    log_level: str = Field(default="INFO", description="Log level (DEBUG/INFO/WARNING/ERROR)")
    log_json: bool = Field(default=True, description="Emit logs as JSON via structlog")

    registry_path: str = Field(
        default="./registry.yaml",
        description="Path to the customer registry YAML file",
    )

    dify_timeout_s: float = Field(
        default=60.0,
        gt=0,
        description="HTTP timeout for Dify Service/Console API calls",
    )
    dify_stream_timeout_s: float = Field(
        default=300.0,
        gt=0,
        description="HTTP timeout for streaming chat-messages (longer than blocking)",
    )

    app_cache_ttl_s: int = Field(
        default=7 * 24 * 3600,
        gt=0,
        description="Idle TTL for cached (customer, model) -> Dify App entries",
    )
    app_cache_gc_interval_s: int = Field(
        default=3600,
        gt=0,
        description="Interval between GC sweeps over the App cache",
    )

    request_id_header: str = Field(
        default="x-request-id",
        description="Header name to read/echo for distributed tracing",
    )

    strict_startup: bool = Field(
        default=False,
        description=(
            "When True, the startup health check (registry format + Dify "
            "reachability + console / dataset auth round-trip) aborts boot "
            "on any failure. When False (default), failures are logged but "
            "the gateway keeps serving — suitable for dev where Dify may "
            "come up after the gateway. Set GATEWAY_STRICT_STARTUP=1 in "
            "production."
        ),
    )

    # --- Rate limiting (PR #7 / Phase 1a) ------------------------------- #
    # Per-tenant requests-per-minute token bucket enforced in middleware.
    # Edge nodes share one finite vLLM; without this, a single runaway /
    # abused customer can starve the rest. See the Edge AI Rate Limiting
    # design doc for the full picture (TPM + cost admission land in 1b).
    rate_limit_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for per-tenant rate limiting. When False, the "
            "RateLimitMiddleware passes every request through untouched — "
            "useful for local dev or a deployment that hasn't tuned limits "
            "yet. Defaults on with a deliberately generous default_rpm so "
            "it protects against runaways without tripping normal traffic."
        ),
    )
    default_rpm: int = Field(
        default=120,
        ge=1,
        description=(
            "Default per-customer requests-per-minute, applied to customers "
            "whose registry entry doesn't set rpm_limit. 120 = 2 req/s "
            "sustained; generous enough that only genuine floods hit it."
        ),
    )
    default_rpm_burst: int = Field(
        default=20,
        ge=1,
        description=(
            "Token-bucket capacity (max instantaneous burst) for the default "
            "RPM limit. Lets bursty-but-low-average clients through while "
            "still capping sustained rate at default_rpm."
        ),
    )
