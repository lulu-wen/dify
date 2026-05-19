"""Lazy-build + cache + GC for per-(customer, model) Dify Apps.

The gateway routes each chat request to a Dify App that bakes in the chosen
LLM model. Because Dify's ``chat-messages`` API does not accept runtime model
overrides, we materialize one App per ``(customer_id, model_id)`` on first use
and cache the resulting App API key.

Lifecycle:
    1. ``get_app_key(customer, model)`` returns the cached App key, or…
    2. …acquires a per-key asyncio lock and builds a fresh App (login →
       DSL import → api-key creation), caches it, and returns the key.
    3. A background sweep evicts entries idle for ``ttl_s`` seconds, deleting
       the corresponding Dify Apps. Errors during GC are logged and swallowed.

JWT lifecycle:
    Console JWTs expire (Dify default ~30 min). We re-login lazily when an
    operation raises a 401-shaped DifyUpstreamError; ``_with_jwt`` wraps each
    console call with one retry-on-auth-failure.

Concurrency notes:
    * Per-key locks prevent duplicate Apps when two requests race for the same
      ``(customer, model)`` pair.
    * The cache is a plain dict guarded by per-key locks; reads are atomic in
      CPython but writes go through the lock to avoid lost updates.
    * The GC task is cooperative: it walks a snapshot of keys, then locks each
      individually before evicting.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from gateway.dify.client import ConsoleSession, DifyClient
from gateway.dify.dsl import build_chat_app_dsl
from gateway.errors import DifyUpstreamError, UnknownModelError
from gateway.registry import CustomerEntry, CustomerRegistry, ModelEntry

logger = structlog.get_logger(__name__)


# Substring used to recognise JWT-expiry style upstream errors. Dify replies
# 401 with ``{"code":"unauthorized",...}``; we are tolerant about exact shape.
_AUTH_FAILURE_HINTS: tuple[str, ...] = ("401", "unauthorized", "expired")


@dataclass
class CachedApp:
    """One Dify App provisioned for a ``(customer_id, model_id)`` pair.

    Timestamps are caller-provided (using the AppManager's injected clock)
    so that tests with a fake clock can deterministically age entries past
    the TTL. Using ``default_factory=time.time`` would tie creation time to
    the real wall clock, which mismatches the GC sweep's view of ``now``
    when a fake clock is in use.
    """

    customer_id: str
    model_id: str
    app_id: str
    app_key: str
    created_at: float
    last_used_at: float


@dataclass
class _CachedSession:
    """Console session entry. ``obtained_at`` is reserved for future
    diagnostics; freshness is enforced by the lock + ``force`` flag in
    :meth:`AppManager._refresh_session`, not by elapsed time."""

    session: ConsoleSession
    obtained_at: float = 0.0


# Type alias for the dependency-injected client factory used by the manager.
ClientFactory = Callable[[CustomerEntry], DifyClient]


class AppManager:
    """Manages per-(customer, model) Dify Apps with lazy build + GC.

    Tests inject a ``client_factory`` to swap in fakes; production wires it to
    a function that returns a singleton :class:`DifyClient` per ``base_url``.
    """

    def __init__(
        self,
        *,
        registry: CustomerRegistry,
        client_factory: ClientFactory,
        ttl_s: int,
        gc_interval_s: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._client_factory = client_factory
        self._ttl_s = ttl_s
        self._gc_interval_s = gc_interval_s
        self._clock = clock

        self._apps: dict[tuple[str, str], CachedApp] = {}
        self._sessions: dict[str, _CachedSession] = {}

        # Per-key locks; defaultdict keeps the wiring trivial.
        self._app_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        self._gc_task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def get_app_key(self, customer: CustomerEntry, model_id: str) -> str:
        """Return an App key for ``(customer, model_id)``, building if needed.

        Raises:
            UnknownModelError: ``model_id`` is not declared for the customer.
            DifyUpstreamError: build failed.
        """
        model = customer.find_model(model_id)
        if model is None:
            raise UnknownModelError(f"model '{model_id}' is not enabled for this customer")

        cache_key = (customer.customer_id, model.id)

        # Fast path: cached.
        cached = self._apps.get(cache_key)
        if cached is not None:
            cached.last_used_at = self._clock()
            return cached.app_key

        # Slow path: build under a lock.
        async with self._app_locks[cache_key]:
            # Re-check after acquiring lock (another task may have built it).
            cached = self._apps.get(cache_key)
            if cached is not None:
                cached.last_used_at = self._clock()
                return cached.app_key

            cached = await self._build_app(customer, model)
            self._apps[cache_key] = cached
            return cached.app_key

    async def start(self) -> None:
        """Launch the background GC task. Call after asyncio loop is running."""
        if self._gc_task is None:
            self._gc_task = asyncio.create_task(self._gc_loop(), name="app-manager-gc")

    async def stop(self) -> None:
        """Cancel GC and best-effort shut everything down."""
        self._stopped = True
        if self._gc_task is not None:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except (asyncio.CancelledError, Exception):
                pass
            self._gc_task = None

    def cached_apps(self) -> dict[tuple[str, str], CachedApp]:
        """Return a *copy* of the cache (for diagnostics / metrics)."""
        return dict(self._apps)

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    async def _build_app(self, customer: CustomerEntry, model: ModelEntry) -> CachedApp:
        client = self._client_factory(customer)
        dsl = build_chat_app_dsl(
            name=f"auto:{customer.customer_id}:{model.id}",
            description=f"Auto-built by AI SDK Gateway for customer={customer.customer_id} model={model.id}",
            provider=model.provider,
            model_name=model.name,
            completion_params=model.completion_params,
            knowledge_base_ids=customer.knowledge_bases,
        )

        # Login (or refresh) → import → key
        async def import_app(session: ConsoleSession) -> str:
            return await client.console_import_app(session, dsl)

        app_id = await self._with_session(customer, client, import_app)

        async def make_key(session: ConsoleSession) -> str:
            return await client.console_create_app_api_key(session, app_id)

        app_key = await self._with_session(customer, client, make_key)

        logger.info(
            "app_manager.built",
            customer_id=customer.customer_id,
            model_id=model.id,
            app_id=app_id,
        )
        # IMPORTANT: timestamps must come from ``self._clock`` (not real
        # time.time) so that GC sweep arithmetic stays consistent under
        # injected test clocks.
        now = self._clock()
        return CachedApp(
            customer_id=customer.customer_id,
            model_id=model.id,
            app_id=app_id,
            app_key=app_key,
            created_at=now,
            last_used_at=now,
        )

    async def _with_session(
        self,
        customer: CustomerEntry,
        client: DifyClient,
        op: Callable[[ConsoleSession], Awaitable[str]],
    ) -> str:
        """Run ``op(session)``; on auth-shaped failure, re-login and retry once."""
        session = await self._get_session(customer, client)
        try:
            return await op(session)
        except DifyUpstreamError as e:
            msg = str(e).lower()
            if not any(hint in msg for hint in _AUTH_FAILURE_HINTS):
                raise
            # Cookies likely expired; force a fresh login (don't return the
            # cached-but-failing session).
            session = await self._refresh_session(customer, client, force=True)
            return await op(session)

    async def _get_session(self, customer: CustomerEntry, client: DifyClient) -> ConsoleSession:
        cached = self._sessions.get(customer.customer_id)
        if cached is not None:
            return cached.session
        return await self._refresh_session(customer, client)

    async def _refresh_session(
        self,
        customer: CustomerEntry,
        client: DifyClient,
        *,
        force: bool = False,
    ) -> ConsoleSession:
        """Acquire or renew the console session.

        Args:
            force: When False (default), if another concurrent caller has
                already populated the cache while we were waiting for the
                lock, reuse it (avoids thundering-herd logins). When True,
                always perform a fresh login — used by the auth-retry path
                in :meth:`_with_session`, where the cached session is the
                one that just failed.
        """
        async with self._session_locks[customer.customer_id]:
            cached = self._sessions.get(customer.customer_id)
            if cached is not None and not force:
                return cached.session
            session = await client.console_login(
                customer.dify.console_email,
                customer.dify.console_password,
            )
            self._sessions[customer.customer_id] = _CachedSession(
                session=session,
                obtained_at=self._clock(),
            )
            return session

    # ------------------------------------------------------------------ #
    # GC                                                                 #
    # ------------------------------------------------------------------ #

    async def _gc_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._gc_interval_s)
            except asyncio.CancelledError:
                return
            try:
                await self._gc_sweep()
            except Exception:
                logger.exception("app_manager.gc_failed")

    async def _gc_sweep(self) -> None:
        now = self._clock()
        # Snapshot keys to avoid mutating during iteration.
        keys = list(self._apps.keys())
        for key in keys:
            cached = self._apps.get(key)
            if cached is None:
                continue
            if now - cached.last_used_at < self._ttl_s:
                continue
            await self._evict(key, cached)

    async def _evict(self, key: tuple[str, str], cached: CachedApp) -> None:
        # Lock the per-(customer,model) entry so we don't race with a builder.
        async with self._app_locks[key]:
            current = self._apps.get(key)
            if current is None or current.app_id != cached.app_id:
                return

            customer = self._registry.find_by_customer_id(cached.customer_id)
            deleted = False
            if customer is not None:
                try:
                    client = self._client_factory(customer)

                    async def delete(session: ConsoleSession) -> str:
                        await client.console_delete_app(session, cached.app_id)
                        return ""

                    await self._with_session(customer, client, delete)
                    deleted = True
                except Exception:
                    # GC must never crash the loop. Log and proceed to evict
                    # the cache entry anyway so the next request re-builds.
                    logger.warning(
                        "app_manager.delete_failed",
                        customer_id=cached.customer_id,
                        model_id=cached.model_id,
                        app_id=cached.app_id,
                        exc_info=True,
                    )

            logger.info(
                "app_manager.evicted",
                customer_id=cached.customer_id,
                model_id=cached.model_id,
                app_id=cached.app_id,
                age_s=round(self._clock() - cached.created_at, 1),
                dify_deleted=deleted,
            )
            self._apps.pop(key, None)

