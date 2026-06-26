from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from .config import settings

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _QueuedJob:
    job_func: Callable[..., Awaitable[Any] | Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    max_concurrency: int
    tracked_job_id: str | None = None


@dataclass
class _SchedulerState:
    pending: deque[_QueuedJob] = field(default_factory=deque)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    running: set[asyncio.Task[None]] = field(default_factory=set)
    tracked_tasks: dict[str, set[asyncio.Task[None]]] = field(default_factory=dict)
    task_job_ids: dict[asyncio.Task[None], str] = field(default_factory=dict)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


_scheduler_states: WeakKeyDictionary[asyncio.AbstractEventLoop, _SchedulerState] = (
    WeakKeyDictionary()
)


def schedule_job(
    job_func: Callable[..., Awaitable[Any] | Any],
    *args: Any,
    max_concurrency: int | None = None,
    tracked_job_id: str | None = None,
    **kwargs: Any,
) -> asyncio.Task[None]:
    state = _scheduler_state()
    queued_job = _QueuedJob(
        job_func=job_func,
        args=args,
        kwargs=kwargs,
        max_concurrency=max(1, max_concurrency or settings.translation_concurrency),
        tracked_job_id=tracked_job_id,
    )
    task = asyncio.create_task(_run_queued_job(state, queued_job))
    state.tasks.add(task)
    if tracked_job_id:
        state.tracked_tasks.setdefault(tracked_job_id, set()).add(task)
        state.task_job_ids[task] = tracked_job_id
    task.add_done_callback(lambda finished: _handle_finished_task(state, finished))
    return task


def cancel_scheduled_job(job_id: str) -> bool:
    """Cancel queued or running scheduler tasks associated with a job id."""
    state = _scheduler_state()
    tasks = state.tracked_tasks.get(job_id, set())
    canceled = False
    for task in list(tasks):
        if task.done():
            continue
        task.cancel()
        canceled = True
    return canceled


def is_job_active(job_id: str) -> bool:
    state = _scheduler_state()
    tasks = state.tracked_tasks.get(job_id, set())
    return any(not task.done() for task in tasks)


def running_job_count() -> int:
    return sum(len(state.running) for state in _scheduler_states.values())


def queued_job_count() -> int:
    return sum(len(state.pending) for state in _scheduler_states.values())


def _scheduler_state() -> _SchedulerState:
    loop = asyncio.get_running_loop()
    state = _scheduler_states.get(loop)
    if state is None:
        state = _SchedulerState()
        _scheduler_states[loop] = state
    return state


async def _run_queued_job(state: _SchedulerState, queued_job: _QueuedJob) -> None:
    current_task = asyncio.current_task()
    if current_task is None:
        raise RuntimeError("Scheduled job is not running in an asyncio task")
    await _acquire_job_slot(state, queued_job, current_task)
    try:
        await _run_job(queued_job.job_func, *queued_job.args, **queued_job.kwargs)
    finally:
        async with state.condition:
            state.running.discard(current_task)
            state.condition.notify_all()


async def _acquire_job_slot(
    state: _SchedulerState,
    queued_job: _QueuedJob,
    task: asyncio.Task[None],
) -> None:
    async with state.condition:
        state.pending.append(queued_job)
        state.condition.notify_all()
        try:
            while True:
                if (
                    state.pending
                    and state.pending[0] is queued_job
                    and len(state.running) < queued_job.max_concurrency
                ):
                    state.pending.popleft()
                    state.running.add(task)
                    state.condition.notify_all()
                    return
                await state.condition.wait()
        except BaseException:
            try:
                state.pending.remove(queued_job)
            except ValueError:
                pass
            state.condition.notify_all()
            raise


async def _run_job(
    job_func: Callable[..., Awaitable[Any] | Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    result = job_func(*args, **kwargs)
    if result is not None:
        await result


def _handle_finished_task(
    state: _SchedulerState,
    task: asyncio.Task[None],
) -> None:
    state.tasks.discard(task)
    tracked_job_id = state.task_job_ids.pop(task, None)
    if tracked_job_id:
        tracked_tasks = state.tracked_tasks.get(tracked_job_id)
        if tracked_tasks is not None:
            tracked_tasks.discard(task)
            if not tracked_tasks:
                state.tracked_tasks.pop(tracked_job_id, None)
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Scheduled job task was cancelled")
    except Exception:
        logger.exception("Scheduled job task failed")
