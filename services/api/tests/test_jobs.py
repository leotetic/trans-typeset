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
