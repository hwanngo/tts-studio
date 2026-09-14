"""Small durable-job task scheduler used by Core generation orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class GenerationScheduler:
    """Track one asyncio task per durable Generation Job.

    The scheduler deliberately knows nothing about Workers or persistence.  The
    service owns those boundaries; this class only keeps queued work alive and
    provides bounded lifecycle cleanup.
    """

    def __init__(self, runner: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        self._runner = runner
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def submit(self, job_id: str) -> None:
        if self._closed:
            raise RuntimeError("generation scheduler is closed")
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        task = asyncio.create_task(self._runner(job_id), name=f"generation-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda finished: self._discard_done(job_id, finished))

    @property
    def tracked_count(self) -> int:
        return len(self._tasks)

    async def wait(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _discard_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
