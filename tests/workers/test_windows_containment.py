"""Exercise the Windows launch gate on every host and native Jobs on Windows."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from tts_studio.workers import windows_job


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment_fails", [False, True])
async def test_worker_cannot_execute_before_job_assignment(tmp_path: Path, assignment_fails: bool):
    marker = tmp_path / "started"

    class Job:
        def assign(self, pid):
            assert not marker.exists()
            if assignment_fails:
                raise OSError("assignment denied")

        def close(self):
            pass

    command = (
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
        str(marker),
    )
    if assignment_fails:
        with pytest.raises(OSError, match="assignment denied"):
            await windows_job.launch_in_job(command, job_factory=Job)
        assert not marker.exists()
    else:
        process = await windows_job.launch_in_job(command, job_factory=Job)
        await asyncio.wait_for(process.wait(), 5)
        assert marker.exists()
        windows_job.job_for(process).close()


@pytest.mark.asyncio
async def test_job_termination_keeps_handle_until_all_descendants_exit():
    class Job:
        active = 1
        closed = False

        def assign(self, pid):
            pass

        def terminate(self):
            pass  # The OS can accept termination while members are still exiting.

        def active_processes(self):
            return self.active

        def close(self):
            self.closed = True

    job = Job()
    process = await windows_job.launch_in_job(
        (sys.executable, "-c", "pass"), job_factory=lambda: job
    )
    await asyncio.wait_for(process.wait(), 5)
    with pytest.raises(TimeoutError):
        await windows_job.terminate_job_process(process, timeout=0.02)
    assert windows_job.job_for(process) is job
    assert not job.closed
    job.active = 0
    await windows_job.terminate_job_process(process, timeout=1)
    assert job.closed
    assert windows_job.job_for(process) is None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
async def test_native_job_terminates_worker_and_descendant(tmp_path: Path):
    marker = tmp_path / "descendant.pid"
    command = (
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
        ),
        str(marker),
    )
    process = await windows_job.launch_in_job(command)
    job = windows_job.job_for(process)
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        assert job.active_processes() >= 3  # trusted gate, Worker, helper
        await windows_job.terminate_job_process(process, timeout=5)
        assert process.returncode is not None
    finally:
        job.close()
