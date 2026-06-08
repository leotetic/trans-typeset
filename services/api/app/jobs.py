from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_running_tasks: set[asyncio.Task[None]] = set()


def schedule_job(
    job_func: Callable[..., Awaitable[Any] | Any],
    *args: Any,
    **kwargs: Any,
) -> asyncio.Task[None]:
    task = asyncio.create_task(_run_job(job_func, *args, **kwargs))
    _running_tasks.add(task)
    task.add_done_callback(_handle_finished_task)
    return task


def running_job_count() -> int:
    return len(_running_tasks)


async def _run_job(
    job_func: Callable[..., Awaitable[Any] | Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    result = job_func(*args, **kwargs)
    if result is not None:
        await result


def _handle_finished_task(task: asyncio.Task[None]) -> None:
    _running_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Scheduled job task was cancelled")
    except Exception:
        logger.exception("Scheduled job task failed")
