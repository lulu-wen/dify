"""FastAPI application entry — wires middleware, routers, and lifespan hooks."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from gateway.config import Settings
from gateway.dify.app_manager import AppManager
from gateway.dify.client import DifyClient
from gateway.errors import GatewayError, InvalidRequestError
from gateway.lifecycle import TaskSupervisor, safe_shutdown_step
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.body_size import BodySizeLimitMiddleware
from gateway.middleware.logging import LoggingMiddleware, configure_logging
from gateway.middleware.rate_limit import RateLimitMiddleware
from gateway.ratelimit import (
    InMemoryQuotaStore,
    InMemoryTokenBucketLimiter,
    QuotaStore,
    RateLimiter,
)
from gateway.ratelimit.headroom import EwmaHeadroomCalculator, HeadroomConfig
from gateway.ratelimit.runtime_metrics import (
    RuntimeMetrics,
    VLLMPrometheusMetrics,
    run_metrics_poll_loop,
)
from gateway.registry import CustomerEntry, CustomerRegistry
from gateway.routers import audio as audio_router
from gateway.routers import chat as chat_router
from gateway.routers import chat_hybrid as chat_hybrid_router
from gateway.routers import chat_thin_proxy as chat_thin_proxy_router
from gateway.routers import datasets as datasets_router
from gateway.routers import embeddings as embeddings_router
from gateway.routers import files as files_router
from gateway.routers import models as models_router
from gateway.startup_check import run_startup_check

logger = structlog.get_logger(__name__)


def _build_dify_client_factory(
    settings: Settings,
    cache: dict[str, DifyClient],
) -> Callable[[CustomerEntry], DifyClient]:
    """Return a function that yields a singleton ``DifyClient`` per ``base_url``."""

    def factory(customer: CustomerEntry) -> DifyClient:
        url = customer.dify.base_url
        existing = cache.get(url)
        if existing is not None:
            return existing
        new_client = DifyClient(
            base_url=url,
            timeout_s=settings.dify_timeout_s,
            stream_timeout_s=settings.dify_stream_timeout_s,
        )
        cache[url] = new_client
        return new_client

    return factory


def create_app(
    settings: Settings | None = None,
    *,
    registry: CustomerRegistry | None = None,
    rate_limiter: RateLimiter | None = None,
    quota_store: QuotaStore | None = None,
    runtime_metrics: RuntimeMetrics | None = None,
) -> FastAPI:
    """Application factory used by ``uvicorn`` and tests.

    Args:
        settings: optional pre-built Settings (defaults: read env).
        registry: optional pre-built registry (tests inject; production loads
            from ``settings.registry_path``).
        rate_limiter: optional pre-built limiter (tests inject one with a
            controllable clock or a recording fake; production builds the
            default in-memory token bucket).
        quota_store: optional pre-built node-admission store (tests inject to
            assert pre-charge/refund; production builds the in-memory one
            sized to ``settings.node_token_budget``).
        runtime_metrics: optional pre-built metrics source (tests inject a
            :class:`MockRuntimeMetrics`; production builds
            :class:`VLLMPrometheusMetrics` from ``settings.runtime_metrics_url``
            when ``settings.runtime_metrics_enabled`` is True). Off by
            default — Phase 2a (PR #9).
    """
    settings = settings or Settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    # PR #13 R1 #8 + PR #14: any mode that exposes thin-proxy chat
    # (``thin_proxy`` or ``hybrid``) REQUIRES a non-empty llm_endpoint —
    # without it every direct-LLM request would hit ServiceUnavailableError.
    # Fail at startup so operators see the misconfiguration immediately,
    # not on first traffic.
    #
    # PR #15 R1 #8: extend the same fail-fast principle to ASR/TTS in
    # modes that mount the audio routes (``thin_proxy`` and ``hybrid``).
    # Previously startup let an audio-route-mounted gateway boot with
    # empty asr/tts endpoints, then every /v1/audio/* request 503'd at
    # runtime — exactly the deferred-failure pattern PR #13 R1 #8 was
    # supposed to close. The asr/tts checks are advisory (warn, don't
    # abort) so a chat-only operator who hasn't wired audio infra
    # doesn't get blocked from starting; a missing endpoint that the
    # operator does intend to use later still surfaces clearly.
    if settings.effective_mode in ("thin_proxy", "hybrid") and not settings.llm_endpoint.strip():
        raise RuntimeError(
            f"mode={settings.effective_mode!r} requires GATEWAY_LLM_ENDPOINT to be set "
            "(empty value would 503 every direct-LLM chat request)."
        )
    if settings.effective_mode in ("thin_proxy", "hybrid"):
        for name, value in (
            ("GATEWAY_ASR_ENDPOINT", settings.asr_endpoint),
            ("GATEWAY_TTS_ENDPOINT", settings.tts_endpoint),
        ):
            if not value.strip():
                logger.warning(
                    "gateway.startup.audio_endpoint_unset",
                    env_var=name,
                    note=(
                        "audio routes are mounted but this endpoint is empty; "
                        "/v1/audio/* requests for this service will 503 until set"
                    ),
                )

    registry = registry or CustomerRegistry.from_yaml(settings.registry_path)
    logger.info("gateway.bootstrap", customers=len(registry))

    dify_clients: dict[str, DifyClient] = {}
    factory = _build_dify_client_factory(settings, dify_clients)
    app_manager = AppManager(
        registry=registry,
        client_factory=factory,
        ttl_s=settings.app_cache_ttl_s,
        gc_interval_s=settings.app_cache_gc_interval_s,
        # Bound generation on auto-built Apps so the node-budget admission
        # reservation is a true upper bound (PR #8 1b). Only injected when
        # rate limiting is enabled — a disabled limiter shouldn't silently
        # cap generation length.
        default_max_output_tokens=(
            settings.default_max_output_tokens if settings.rate_limit_enabled else 0
        ),
        # Same gating for RAG retrieval cap: when rate limiting is on, cap
        # Dify ``dataset_configs.top_k`` so the reservation's RAG allowance
        # (top_k * chunk_tokens) bounds the retrieved context for real, not
        # just on paper (codex 1b review-5 P2).
        retrieval_top_k=(
            settings.default_kb_top_k if settings.rate_limit_enabled else None
        ),
    )

    # PR #11: HeadroomConfig is constructed UNCONDITIONALLY (even if
    # runtime_metrics is disabled). This validates the config at startup
    # — bad headroom thresholds in the env raise ValueError here, before
    # the gateway starts accepting traffic. Replaces the @model_validator
    # that previously lived on Settings (which only fired for env-loaded
    # construction, missing test fixtures + Phase 4 hot-reload paths).
    headroom_config = HeadroomConfig(
        soft_threshold=settings.headroom_soft_threshold,
        hard_threshold=settings.headroom_hard_threshold,
        ewma_alpha=settings.headroom_ewma_alpha,
    )

    # PR #9: pick the metrics source. Explicit precedence (review-1 #10):
    #   1. Test injection wins outright — ``runtime_metrics`` arg given.
    #   2. Otherwise honour the production setting.
    #   3. Otherwise the feature is off and we don't build the calculator.
    headroom_calc: EwmaHeadroomCalculator | None = None
    metrics_source: RuntimeMetrics | None = None
    if runtime_metrics is not None:
        metrics_source = runtime_metrics
    elif settings.runtime_metrics_enabled:
        metrics_source = VLLMPrometheusMetrics(
            url=settings.runtime_metrics_url,
            timeout_s=settings.runtime_metrics_timeout_s,
        )
    if metrics_source is not None:
        headroom_calc = EwmaHeadroomCalculator(headroom_config)

    # PR #11: per-app TaskSupervisor — manages background-task lifecycle
    # (chat router's fire-and-forget cancel POSTs + the metrics polling
    # loop). Created here so app.state has it from the moment any request
    # could arrive; lifespan ``shutdown`` cancels everything with a
    # deadline + logs escapees.
    task_supervisor = TaskSupervisor()

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        await app_manager.start()
        try:
            # PR #13: thin-proxy mode bypasses Dify entirely — the
            # startup check would fail (or worse, succeed against
            # stale Dify) since the chat/audio routes never call
            # AppManager / DifyClient. Skip the L2/L3/L4 round-trip;
            # the registry's pydantic validation (L1) has already run.
            #
            # PR #14: hybrid mode runs BOTH paths so we still need the
            # Dify health check (some customers will route through Dify
            # for RAG). thin_proxy stays the only skip.
            if settings.effective_mode == "thin_proxy":
                logger.info(
                    "gateway.startup.thin_proxy_mode",
                    llm_endpoint=settings.llm_endpoint,
                    asr_endpoint=settings.asr_endpoint,
                    tts_endpoint=settings.tts_endpoint,
                )
            else:
                # PR #5: validate registry against real Dify deployments
                # before accepting traffic. Raises RuntimeError (and so
                # aborts uvicorn startup with non-zero exit) when
                # settings.strict_startup is True; otherwise logs warnings
                # and continues.
                #
                # Codex review-3 P2: read the factory from app.state at
                # call time, NOT from the closure captured during
                # create_app. Tests (see ``conftest.py::app``) replace
                # ``app.state.dify_client_factory`` AFTER ``create_app``
                # returns so requests hit a FakeDifyClient instead of
                # doing real HTTP.
                active_factory = app_instance.state.dify_client_factory
                await run_startup_check(
                    registry,
                    active_factory,
                    strict=settings.strict_startup,
                )
            # PR #9: start the headroom polling task AFTER startup checks
            # pass. PR #11: dispatched through task_supervisor so shutdown
            # gathers it with the chat router's cancel POSTs in one
            # bounded await.
            if metrics_source is not None and headroom_calc is not None:
                task_supervisor.spawn_long_running(
                    run_metrics_poll_loop(
                        metrics=metrics_source,
                        calculator=headroom_calc,
                        quota_store=app_instance.state.quota_store,
                        static_budget=settings.node_token_budget,
                        poll_interval_s=settings.runtime_metrics_poll_s,
                    ),
                    name="runtime-metrics-poll",
                )
            yield
        finally:
            # PR #11: every shutdown step goes through ``safe_shutdown_step``
            # so a bug in any cleanup surfaces via ``logger.exception`` with
            # the step name (instead of the previous silent
            # ``except (CancelledError, Exception): pass`` swallow).
            await safe_shutdown_step(
                "task_supervisor", task_supervisor.shutdown(deadline_s=5.0)
            )
            if isinstance(metrics_source, VLLMPrometheusMetrics):
                await safe_shutdown_step(
                    "metrics_source.aclose", metrics_source.aclose()
                )
            await safe_shutdown_step("app_manager.stop", app_manager.stop())
            for url, client in dify_clients.items():
                await safe_shutdown_step(
                    f"dify_client.aclose:{url}", client.aclose()
                )
            logger.info("gateway.shutdown")

    app = FastAPI(
        title="AI SDK Gateway",
        version="0.1.0",
        description="OpenAI-compatible gateway routing to per-customer Dify deployments.",
        lifespan=lifespan,
    )

    rate_limiter = rate_limiter or InMemoryTokenBucketLimiter()
    quota_store = quota_store or InMemoryQuotaStore(
        node_token_budget=settings.node_token_budget
    )

    app.state.settings = settings
    app.state.registry = registry
    app.state.app_manager = app_manager
    app.state.dify_client_factory = factory
    app.state.dify_clients = dify_clients
    app.state.rate_limiter = rate_limiter
    app.state.quota_store = quota_store
    app.state.runtime_metrics = metrics_source
    app.state.headroom_calculator = headroom_calc
    app.state.task_supervisor = task_supervisor

    # Middleware ordering (add order is INNERMOST-first in Starlette, so the
    # last added runs outermost):
    #   request flow:  CORS -> Logging -> BodySize -> Auth -> RateLimit -> route
    # - CORS outermost: preflight ``OPTIONS`` requests must be answered with
    #   ``Access-Control-Allow-Origin`` BEFORE any auth check. Auth middleware
    #   would 401 the preflight (no Authorization header on an OPTIONS) and
    #   the browser would never send the real request.
    # - Logging next: request id exists even on auth / rate-limit failure.
    # - BodySize before Auth: a multi-GB body shouldn't even get to the auth
    #   parser (PR #13 R2 #9). Added AFTER Auth here so it ends up outer.
    # - RateLimit inner to Auth: it keys on request.state.customer, which
    #   AuthMiddleware sets. Added BEFORE Auth here so it ends up inner.
    app.add_middleware(RateLimitMiddleware, limiter=rate_limiter, settings=settings)
    app.add_middleware(AuthMiddleware, registry=registry)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(LoggingMiddleware, request_id_header=settings.request_id_header)
    # PR #13 R2 follow-up: enable CORS when configured. Empty list →
    # middleware not installed (the production deployment behind a single
    # domain may not need it). The demo HTML in scripts/translator_demo.html
    # is served from a different localhost port than the gateway, so dev
    # deployments must set ``GATEWAY_CORS_ALLOW_ORIGINS`` to either ``*``
    # or the explicit dev origins.
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=False,  # Authorization header travels in body, not cookies
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
            max_age=600,  # cache preflight for 10 min to avoid OPTIONS spam
        )

    # Chat router selection per deployment mode (PR #13 + PR #14):
    #   dify       → chat.py (Dify-orchestrated, RAG always on)
    #   thin_proxy → chat_thin_proxy.py (direct to LLM, no RAG) + audio
    #   hybrid     → chat_hybrid.py (per-request dispatch on use_rag) + audio
    #
    # All three modes register exactly one router at /v1/chat/completions
    # so FastAPI never sees a path conflict. The Dify-flavoured datasets /
    # files routes stay mounted unconditionally — clients calling them
    # when no Dify is wired up will get a clean 502 from the path, not a
    # confusing 404.
    mode = settings.effective_mode
    if mode == "thin_proxy":
        app.include_router(chat_thin_proxy_router.router)
        app.include_router(audio_router.router)
    elif mode == "hybrid":
        app.include_router(chat_hybrid_router.router)
        app.include_router(audio_router.router)
    else:  # "dify"
        app.include_router(chat_router.router)
    app.include_router(embeddings_router.router)
    app.include_router(models_router.router)
    app.include_router(datasets_router.router)
    app.include_router(files_router.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "ai-sdk-gateway", "version": app.version}

    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        # Surface Retry-After when the error carries a hint (rate-limit /
        # overload). Header is the standard signal; the body's ``action``
        # field carries the richer hint. Round up so a 0.4s wait still
        # tells the client "wait 1s" rather than "retry immediately".
        headers: dict[str, str] | None = None
        if exc.retry_after_s is not None:
            headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after_s)))}
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_openai_envelope(),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Reshape FastAPI/Pydantic 422 into the OpenAI error envelope (R7).

        FastAPI's default ``{"detail": [...]}`` violates the gateway's
        OpenAI-compatibility contract. Surface a single, actionable message
        derived from the first validation error; the full ``errors`` list is
        included under ``error.errors`` for clients that want detail.
        """
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or None
        message = first.get("msg", "request validation failed")

        wrapper = InvalidRequestError(message, param=loc)
        envelope = wrapper.to_openai_envelope()
        # Attach the full validation report for clients that want it; OpenAI's
        # envelope schema does not forbid extra fields.
        envelope["error"]["errors"] = errors
        return JSONResponse(status_code=wrapper.status_code, content=envelope)

    return app


# For ``uvicorn gateway.main:app``. Tests should call ``create_app(...)`` directly
# with an in-memory registry instead of relying on this module-level instance.
# Construction is deferred until the attribute is accessed by uvicorn so that
# importing this module (e.g. for unit tests of helpers) does not require a
# registry file on disk.
def __getattr__(name: str) -> Any:
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(name)
