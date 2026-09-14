"""Windows Job containment with a trusted gate before any engine code executes.

The gate uses only Core's Python standard library. Adapter code still executes
through its explicit isolated-environment command, as a child inheriting the Job.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from asyncio.subprocess import Process
from collections.abc import Callable, Sequence
from typing import Any, Protocol
from weakref import WeakKeyDictionary

_GATE = (
    "import subprocess,sys; "
    "permit=sys.stdin.buffer.read(1); "
    "sys.exit(subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL) if permit==b'1' else 1)"
)
_KILL_ON_JOB_CLOSE = 0x2000


class JobHandle(Protocol):
    def assign(self, pid: int) -> None: ...
    def terminate(self) -> None: ...
    def active_processes(self) -> int: ...
    def close(self) -> None: ...


_jobs: WeakKeyDictionary[Process, JobHandle] = WeakKeyDictionary()


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class WindowsJob:
    """Own a non-inheritable unnamed Job; never permit process breakaway."""

    def __init__(self) -> None:
        # WinDLL is absent from ctypes and its stubs on non-Windows hosts.
        self._api = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            "SetInformationJobObject": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int,
            ),
            "OpenProcess": ([ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
            "AssignProcessToJobObject": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
            "TerminateJobObject": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            "QueryInformationJobObject": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p],
                ctypes.c_int,
            ),
            "CloseHandle": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._api, name)
            function.argtypes, function.restype = arguments, result
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("Could not create Worker Job Object")
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise OSError("Could not configure Worker Job Object")

    def assign(self, pid: int) -> None:
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE, required by assignment.
        process = self._api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise OSError("Could not open gated Worker process")
        try:
            if not self._api.AssignProcessToJobObject(self._handle, process):
                raise OSError("Could not contain Worker in Job Object")
        finally:
            self._api.CloseHandle(process)

    def terminate(self) -> None:
        if not self._api.TerminateJobObject(self._handle, 1):
            raise OSError("Worker Job Object termination failed")

    def active_processes(self) -> int:
        accounting = _Accounting()
        if not self._api.QueryInformationJobObject(
            self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ):
            raise OSError("Worker Job Object exit could not be verified")
        return int(accounting.ActiveProcesses)

    def close(self) -> None:
        if self._handle:
            if not self._api.CloseHandle(self._handle):
                raise OSError("Worker Job Object handle could not be closed")
            self._handle = None


async def launch_in_job(
    arguments: Sequence[str],
    *,
    job_factory: Callable[[], JobHandle] = WindowsJob,
    **kwargs: Any,
) -> Process:
    """Assign the blocked trusted gate before permitting an engine to spawn."""
    job = job_factory()
    process: Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            _GATE,
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            **kwargs,
        )
        job.assign(process.pid)
        _jobs[process] = job
        assert process.stdin is not None
        process.stdin.write(b"1")
        await process.stdin.drain()
        process.stdin.close()
        return process
    except BaseException:
        try:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(process.wait(), timeout=5)
        finally:
            job.close()
        raise


def job_for(process: Process) -> JobHandle | None:
    return _jobs.get(process)


async def terminate_job_process(process: Process, *, timeout: float) -> None:
    """Keep the handle on failure; only retire it after every member has exited."""
    job = _jobs[process]
    job.terminate()
    async with asyncio.timeout(timeout):
        await process.wait()
        while job.active_processes():
            await asyncio.sleep(0.01)
    job.close()
    del _jobs[process]
