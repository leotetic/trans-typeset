import asyncio

import pytest

from app import jobs


def test_schedule_job_runs_async_callable_on_current_loop() -> None:
    observed: list[int] = []

    async def run() -> None:
        expected_loop = asyncio.get_running_loop()

        async def job() -> None:
            observed.append(id(asyncio.get_running_loop()))

        task = jobs.schedule_job(job)
        await task
        assert observed == [id(expected_loop)]

    asyncio.run(run())


def test_schedule_job_refills_to_configured_concurrency_after_first_wave() -> None:
    starts: list[int] = []

    async def run() -> None:
        first_wave_started = asyncio.Event()
        release_first_wave = asyncio.Event()
        second_wave_started = asyncio.Event()
        release_second_wave = asyncio.Event()

        async def job(index: int) -> None:
            starts.append(index)
            if len(starts) == 4:
                first_wave_started.set()
            if len(starts) == 8:
                second_wave_started.set()
            if index < 4:
                await release_first_wave.wait()
            else:
                await release_second_wave.wait()

        tasks = [jobs.schedule_job(job, index, max_concurrency=4) for index in range(8)]
        await asyncio.wait_for(first_wave_started.wait(), timeout=1)

        assert jobs.running_job_count() == 4
        assert jobs.queued_job_count() == 4

        release_first_wave.set()
        await asyncio.wait_for(second_wave_started.wait(), timeout=1)

        assert jobs.running_job_count() == 4
        assert jobs.queued_job_count() == 0

        release_second_wave.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())

    assert starts[:4] == [0, 1, 2, 3]
    assert starts[4:] == [4, 5, 6, 7]


def test_cancel_tracked_queued_job_removes_it_before_start() -> None:
    starts: list[int] = []

    async def run() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def job(index: int) -> None:
            starts.append(index)
            if index == 0:
                first_started.set()
                await release_first.wait()

        first = jobs.schedule_job(job, 0, max_concurrency=1, tracked_job_id="job_0")
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = jobs.schedule_job(job, 1, max_concurrency=1, tracked_job_id="job_1")
        await asyncio.sleep(0)

        assert jobs.is_job_active("job_1") is True
        assert jobs.cancel_scheduled_job("job_1") is True
        with pytest.raises(asyncio.CancelledError):
            await second
        assert jobs.is_job_active("job_1") is False

        release_first.set()
        await first

    asyncio.run(run())

    assert starts == [0]


def test_cancel_tracked_running_job_cancels_task() -> None:
    async def run() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def job() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        task = jobs.schedule_job(job, tracked_job_id="job_1")
        await asyncio.wait_for(started.wait(), timeout=1)

        assert jobs.is_job_active("job_1") is True
        assert jobs.cancel_scheduled_job("job_1") is True
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), timeout=1)
        assert jobs.is_job_active("job_1") is False

    asyncio.run(run())


def test_tracked_job_registry_cleans_up_after_success() -> None:
    async def run() -> None:
        task = jobs.schedule_job(lambda: None, tracked_job_id="job_1")
        await task

        assert jobs.is_job_active("job_1") is False
        assert jobs.cancel_scheduled_job("job_1") is False

    asyncio.run(run())
