import asyncio

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
