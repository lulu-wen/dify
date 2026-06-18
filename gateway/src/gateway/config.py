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

    # --- Cost-based admission + TPM (PR #8 / Phase 1b) ------------------ #
    # The OOM guard: edge nodes are bottlenecked by KV-cache memory, not
    # request rate. A single huge-context request can OOM the box while RPM
    # looks fine — so we also meter token cost (TPM) and gate concurrent
    # in-flight token reservation (node_token_budget). See the Edge AI Rate
    # Limiting design doc, appendix B.
    default_tpm: int = Field(
        default=0,
        ge=0,
        description=(
            "Default per-customer tokens-per-minute. 0 = unlimited (TPM is "
            "opt-in); the node_token_budget admission gate still protects "
            "against OOM. Set a positive value (or per-customer tpm_limit) "
            "to also cap sustained token throughput per tenant."
        ),
    )
    default_tpm_burst: int = Field(
        default=40_000,
        ge=1,
        description=(
            "Token-bucket capacity for the TPM limit. Should be >= the "
            "largest single request's token_cost (input + max_output) you "
            "want to allow, else such a request can never pass TPM. Generous "
            "by default so only sustained heavy throughput trips it."
        ),
    )
    node_token_budget: int = Field(
        default=200_000,
        ge=1,
        description=(
            "Max concurrent in-flight token cost (sum of input + "
            "max_output across all active chat requests) admitted on this "
            "node. The OOM ceiling — a new request that would push the sum "
            "over this is rejected with 503 REJECTED_OVERLOAD. Tune to the "
            "Jetson's actual KV-cache capacity via E2E; 200k is a "
            "conservative starting point."
        ),
    )
    default_max_output_tokens: int = Field(
        default=1024,
        ge=1,
        description=(
            "Fallback max-output used for cost estimation when a chat "
            "request omits max_tokens. Without it, pre-charge would be "
            "unbounded for unbounded-output requests. A conservative cap "
            "for estimation only — it does not change what the client asked "
            "Dify/vLLM for."
        ),
    )
    default_kb_top_k: int = Field(
        default=3,
        ge=1,
        description=(
            "Max number of retrieval chunks Dify injects per chat request "
            "for customers with knowledge bases attached. Set as "
            "``dataset_configs.top_k`` in the auto-built App DSL — so this "
            "is the REAL upper bound on retrieved context, not an estimate. "
            "The admission reservation uses the same value to account for "
            "RAG-injected tokens (codex 1b review-5 P2)."
        ),
    )
    default_kb_chunk_tokens: int = Field(
        default=1000,
        ge=1,
        description=(
            "Conservative upper bound on tokens per retrieval chunk. The "
            "admission reservation adds ``default_kb_top_k * "
            "default_kb_chunk_tokens`` when a customer has KBs attached. "
            "Tune to your Dify ``indexing_technique`` chunk-size setting; "
            "the default suits Dify's high-quality chunking (~500-1000 "
            "tokens). Estimation knob only; Dify chunk size is set at "
            "indexing time."
        ),
    )

    # ------------------------------------------------------------------ #
    # PR #9 (Phase 2a): live vLLM metrics + headroom-driven admission     #
    # ------------------------------------------------------------------ #
    runtime_metrics_enabled: bool = Field(
        default=False,
        description=(
            "When True, a background asyncio task polls "
            "``runtime_metrics_url`` and scales the effective node budget "
            "against live ``gpu_cache_usage_perc`` (PR #9 / Phase 2a). "
            "Off by default — opt-in until operators have run E2E to "
            "tune thresholds for their Jetson + model combo. When off, "
            "the static ``node_token_budget`` rules unchanged."
        ),
    )
    runtime_metrics_url: str = Field(
        default="http://localhost:8000/metrics",
        description=(
            "vLLM Prometheus endpoint. Default matches the dev "
            "docker-compose vLLM port. Must serve Prometheus text format."
        ),
    )
    runtime_metrics_poll_s: float = Field(
        default=2.0,
        gt=0.0,
        description=(
            "How often to poll vLLM /metrics. 2s balances reactivity to "
            "edge bursts (LLM inference can fill KV in seconds) against "
            "vLLM-side cost of serving the scrape. Lower values waste "
            "CPU; higher values let a spike commit OOM before we react."
        ),
    )
    runtime_metrics_timeout_s: float = Field(
        default=2.0,
        gt=0.0,
        description=(
            "HTTP timeout for each /metrics fetch. Decoupled from "
            "``runtime_metrics_poll_s`` so an operator picking a "
            "sub-second poll (high-frequency monitoring) doesn't get a "
            "matching sub-second timeout that fails on every jitter "
            "spike (PR #9 review-1 #8). Tune separately when the vLLM "
            "scrape regularly exceeds the default."
        ),
    )
    headroom_soft_threshold: float = Field(
        default=0.80,
        ge=0.0,
        lt=1.0,
        description=(
            "GPU KV-cache fraction at which the effective budget starts "
            "scaling DOWN. usage <= soft → full budget; soft < usage < "
            "hard → linear ramp; usage >= hard → 0 budget. 0.80 leaves a "
            "20-point cushion before reject-all kicks in."
        ),
    )
    headroom_hard_threshold: float = Field(
        default=0.95,
        gt=0.0,
        le=1.0,
        description=(
            "GPU KV-cache fraction at which the effective budget is 0 "
            "(reject all new admits). 5% gap to 1.0 reserves headroom for "
            "in-flight generations to extend KV as context grows — "
            "admitting at >95% pushes the next token into OOM or paged "
            "attention, both of which spike TTFT."
        ),
    )
    headroom_ewma_alpha: float = Field(
        default=0.3,
        gt=0.0,
        le=1.0,
        description=(
            "EWMA smoothing for raw cache_usage readings before scaling. "
            "alpha=0.3 reaches ~70%% of a step change in 3-4 polls (6-8s at "
            "2s poll). Higher = more reactive (track bursts faster, but "
            "noisier); lower = smoother (ignores spikes but slow to "
            "respond)."
        ),
    )

    # PR #11: cross-field validation moved to
    # :class:`gateway.ratelimit.headroom.HeadroomConfig.__post_init__`
    # so EVERY construction path (env load, test fixture, Phase 4 hot-
    # reload) is covered by one source of truth. ``create_app`` constructs
    # HeadroomConfig unconditionally at startup so config errors STILL
    # surface during Lifespan startup, not at first request.

    # ------------------------------------------------------------------ #
    # PR #13 (Phase A1): thin-proxy mode for EMS-managed AI services      #
    # ------------------------------------------------------------------ #
    # When EMS provides LLM/ASR/TTS as separate equipment (Dify is just
    # one option among many), the gateway becomes a pure routing layer:
    # auth + rate-limit + schema-translate + forward. Dify orchestration
    # becomes opt-in per customer. See Notion ``EMS Integration``.

    thin_proxy_mode: bool = Field(
        default=False,
        description=(
            "When True, the gateway becomes a thin proxy: chat completions "
            "forward directly to ``llm_endpoint`` (bypassing Dify, no "
            "App lazy-build), the new /v1/audio/* routes activate, and "
            "startup health check skips Dify reachability since there "
            "may be no Dify at all. Customers in registry.yaml can still "
            "carry ``dify`` config for the existing path (per-request "
            "override TBD); when ``dify`` is omitted the customer is "
            "thin-proxy-only and chat goes direct to LLM."
        ),
    )
    llm_endpoint: str = Field(
        default="",
        description=(
            "EMS-provided LLM endpoint base URL. Reached as "
            "``{llm_endpoint}/v1/chat/completions`` (OpenAI-compatible). "
            "Must be set when thin_proxy_mode=true and any customer "
            "lacks a ``dify`` config. Example: ``http://100.88.9.9/llm``."
        ),
    )
    asr_endpoint: str = Field(
        default="",
        description=(
            "EMS-provided ASR endpoint base URL (WhisperX). Reached as "
            "``{asr_endpoint}/transcribe`` (multipart). The gateway's "
            "/v1/audio/transcriptions translates the OpenAI Whisper "
            "schema to WhisperX's native form. Example: "
            "``http://100.88.9.9/asr``."
        ),
    )
    tts_endpoint: str = Field(
        default="",
        description=(
            "EMS-provided TTS endpoint base URL (Kokoro). Already OpenAI-"
            "compatible; the gateway's /v1/audio/speech is pure "
            "passthrough. Example: ``http://100.88.9.9/tts``."
        ),
    )
    ems_request_timeout_s: float = Field(
        default=300.0,
        gt=0,
        description=(
            "HTTP timeout for thin-proxy forwarding to EMS endpoints. "
            "Generous default because WhisperX batch transcription can "
            "take tens of seconds for long audio."
        ),
    )
    # PR #13 R2 #6: optional upstream bearer keys. EMS deployments hardened
    # with LITELLM_MASTER_KEY (a standard prod recommendation) would 401
    # every request without this. Default empty = no Authorization header
    # sent, which suits the Tailscale-CGNAT-isolated PoC setup but isn't
    # safe to assume in general production.
    llm_api_key: str = Field(
        default="",
        description=(
            "Bearer token for the EMS LLM endpoint. When set, the chat "
            "thin-proxy router sends ``Authorization: Bearer <key>``. "
            "Leave empty for Tailscale-isolated deployments."
        ),
    )
    asr_api_key: str = Field(
        default="",
        description="Bearer token for the EMS ASR endpoint. See ``llm_api_key``.",
    )
    tts_api_key: str = Field(
        default="",
        description="Bearer token for the EMS TTS endpoint. See ``llm_api_key``.",
    )
    max_body_bytes: int = Field(
        default=25 * 1024 * 1024,  # 25 MB
        ge=0,
        description=(
            "Request body size cap enforced by BodySizeLimitMiddleware "
            "(PR #13 R2 #9). Set to 0 to disable the cap (tests only). "
            "Default 25 MB suits chat / embeddings / short ASR clips; "
            "raise when on-prem deployments need long-form audio uploads."
        ),
    )
