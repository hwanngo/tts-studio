"""Installed protocol-package typing contract."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _environment_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def test_protocol_wheel_exposes_message_and_rpc_types_to_external_mypy(tmp_path: Path) -> None:
    distribution_dir = tmp_path / "dist"
    environment = tmp_path / "consumer-environment"
    consumer = tmp_path / "consumer.py"

    subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "tts-studio-protocol",
            "--wheel",
            "--out-dir",
            str(distribution_dir),
        ],
        cwd=_REPOSITORY_ROOT,
        check=True,
    )
    wheels = tuple(distribution_dir.glob("tts_studio_protocol-*.whl"))
    assert len(wheels) == 1

    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        cwd=tmp_path,
        check=True,
    )
    installed_python = _environment_python(environment)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(installed_python),
            f"mypy=={version('mypy')}",
            str(wheels[0]),
        ],
        cwd=tmp_path,
        check=True,
    )

    consumer.write_text(
        """\
from typing import cast

import grpc
from google.protobuf.message import Message
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc

channel: grpc.aio.Channel = object()
protobuf_message: Message = object()
request: engine_pb2.DescribeRequest = 1
servicer: engine_pb2_grpc.EngineWorkerServicer = object()

sync_channel = cast(grpc.Channel, object())
async_channel = cast(grpc.aio.Channel, object())
sync_stub = engine_pb2_grpc.EngineWorkerStub(sync_channel)
async_stub = engine_pb2_grpc.EngineWorkerStub(async_channel)

sync_stub_must_not_be_any: int = sync_stub
async_stub_must_not_be_any: int = async_stub
sync_result_must_be_describe_response: engine_pb2.HealthResponse = sync_stub.Describe(
    engine_pb2.DescribeRequest()
)
async_result_must_be_describe_response: engine_pb2.HealthResponse = async_stub.Describe(
    engine_pb2.DescribeRequest()
)
sync_stub.Describe(engine_pb2.HealthRequest())
async_stub.Describe(engine_pb2.HealthRequest())
""",
        encoding="utf-8",
    )
    consumer_environment = os.environ.copy()
    consumer_environment.pop("MYPYPATH", None)
    consumer_environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(installed_python),
            "-m",
            "mypy",
            "--strict",
            "--no-error-summary",
            str(consumer),
        ],
        cwd=tmp_path,
        env=consumer_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert output.count("error: Incompatible types in assignment") == 8, output
    assert output.count('has incompatible type "HealthRequest"; expected "DescribeRequest"') == 2, (
        output
    )
    assert "EngineWorkerStub" in output
    assert "EngineWorkerAsyncStub" in output
    assert "DescribeResponse" in output
    assert "UnaryUnaryMultiCallable" in output
    assert "missing library stubs or py.typed marker" not in output
    assert "[import-untyped]" not in output
