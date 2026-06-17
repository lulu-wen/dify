"""Background-task lifecycle + shutdown-step helpers (PR #11).

Replaces three duplicated patterns shipped across PRs #9 + #10:

1. The module-level ``_bg_cancel_tasks`` set + ``_fire_and_forget`` helper
   in :mod:`gateway.routers.chat` (PR #10).
2. The inline ``metrics_task = asyncio.create_task(...)`` +
   ``metrics_task.cancel()`` + ``await metrics_task`` block in
   :mod:`gateway.main` lifespan (PR #9).
3. The ``try/except (CancelledError, Exception): pass`` pattern in
   :meth:`AppManager.stop`, :func:`gateway.main` lifespan finally, and
   the ``for client in dify_clients.values(): await client.aclose()``
   loop.

Single place to manage background-task lifecycle (per-app, on
``app.state.task_supervisor``) and to wrap shutdown-step cleanup so any
escaping exception lands in ``logger.exception`` rather than being
silently discarded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine
from typing import Any

import structlog

from gateway.observability.metrics import GATEWAY_BACKGROUND_TASKS_PENDING

logger = structlog.get_logger(__name__)


class TaskSupervisor:
    """Per-app background-task lifecycle manager.

    Holds strong references to in-flight tasks so the GC can't drop them
    before completion (a recurring class of ``Task was destroyed but it
    is pending`` warnings) and lets the lifespan cancel everything with
    one ``await`` on shutdown.

    Two kinds of tasks:

    - ``fire_and_forget(coro)`` — fast best-effort calls (e.g. the
      Dify ``chat_messages_stop`` POST from PR #10's disconnect cancel).
      Self-discard via ``done_callback``; never awaited individually.
    - ``spawn_long_running(coro, name=...)`` — long-lived loops (e.g.
      PR #9's ``run_metrics_poll_loop``). Returned task ref is kept so
      the caller can still hold its own handle if needed.

    Both kinds participate in ``shutdown(deadline_s)`` which cancels
    every pending task and awaits them with a bounded deadline.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_count(self) -> int:
        """Number of in-flight tasks (telemetry / Prometheus gauge)."""
        return len(self._tasks)

    def fire_and_forget(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> None:
        """Schedule + track + auto-discard. Replaces the per-router pattern."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        GATEWAY_BACKGROUND_TASKS_PENDING.set(len(self._tasks))
        # PR #12a: include the gauge update in the done_callback chain so the
        # tracked count drops at the same moment as the set discard. Using a
        # lambda over both is fine — they're cheap and unconditional.
        task.add_done_callback(self._on_task_done)

    def spawn_long_running(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Long-running tracked task. Caller may keep the returned ref to
        also hold their own handle (e.g. for explicit cancel). On natural
        completion the task self-discards from the supervisor's set."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        GATEWAY_BACKGROUND_TASKS_PENDING.set(len(self._tasks))
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """Done callback: discard + republish the gauge atomically."""
        self._tasks.discard(task)
        GATEWAY_BACKGROUND_TASKS_PENDING.set(len(self._tasks))

    async def shutdown(self, *, deadline_s: float = 5.0) -> None:
        """Cancel all tracked tasks + await with deadline.

        Logs any non-``CancelledError`` exceptions via ``logger.exception``
        (we go through ``asyncio.gather(..., return_exceptions=True)`` to
        collect everything before logging — so a slow / failing task
        doesn't mask another's bug). After ``deadline_s`` we move on and
        log a warning naming the still-pending tasks; the event loop
        teardown will reap them.
        """
        pending = list(self._tasks)
        if not pending:
            return
        for task in pending:
            task.cancel()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=deadline_s,
            )
        except TimeoutError:
            still_pending = [t for t in pending if not t.done()]
            logger.warning(
                "task_supervisor.shutdown_deadline_exceeded",
                pending=len(still_pending),
                names=[t.get_name() for t in still_pending],
            )
            return
        for task, result in zip(pending, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                logger.exception(
                    "task_supervisor.task_failed_at_shutdown",
                    name=task.get_name(),
                    exc_info=result,
                )


async def safe_shutdown_step(
    name: str,
    coro: Awaitable[Any],
) -> None:
    """Lifespan-finally helper.

    Replaces duplicated patterns:

    .. code-block:: python

        try:
            await app_manager.stop()
        except (asyncio.CancelledError, Exception):
            pass

    with:

    .. code-block:: python

        await safe_shutdown_step("app_manager", app_manager.stop())

    ``CancelledError`` is swallowed (shutdown is the expected reason).
    Any other ``Exception`` is logged via ``logger.exception`` with the
    step ``name`` so a real bug in a cleanup path is visible in the
    post-mortem instead of being silently discarded (PR #9 R2 #5 +
    PR #10 R2 #3).
    """
    try:
        await coro
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("shutdown_step_failed", step=name)
