from __future__ import annotations

import ast
import asyncio
import textwrap
from pathlib import Path

import grpc
import pytest
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc


def _hardware_script() -> str:
    hardware_test = Path(__file__).with_name("test_vieneu_hardware.py")
    source = hardware_test.read_text(encoding="utf-8")
    # Inspect the literal's value independently of formatter quote choices.
    return next(
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "script" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def test_vieneu_hardware_script_parses_before_the_opt_in_gate_runs() -> None:
    compile(textwrap.dedent(_hardware_script()), "hardware-gate", "exec")


@pytest.mark.asyncio
async def test_hardware_cancellation_block_consumes_a_real_grpc_stream() -> None:
    # Execute the actual embedded call site, without importing VieNeu or requiring
    # model files. A syntax-only check cannot detect grpc's async-iterable API.
    tree = ast.parse(_hardware_script())
    variants = next(node for node in ast.walk(tree) if isinstance(node, ast.For))
    start = next(
        index
        for index, node in enumerate(variants.body)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "cancelled_stream"
            for target in node.targets
        )
    )
    end = next(
        index
        for index, node in enumerate(variants.body[start + 1 :], start + 1)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and isinstance(node.value.value.func, ast.Attribute)
        and node.value.value.func.attr == "UnloadModel"
    )
    wrapper = ast.parse("async def probe(stub, metadata, voices): pass")
    wrapper.body[0].body = variants.body[start:end]
    namespace = {"asyncio": asyncio, "grpc": grpc, "engine_pb2": engine_pb2}
    # Execute only the checked-in test script, never cache or external input.
    exec(  # noqa: S102
        compile(ast.fix_missing_locations(wrapper), "hardware-cancellation", "exec"), namespace
    )

    cancelled = asyncio.Event()

    class StreamingWorker(engine_pb2_grpc.EngineWorkerServicer):
        async def Synthesize(self, request, context):
            try:
                yield engine_pb2.SynthesisEvent(header=engine_pb2.AudioHeader(sample_rate_hz=48000))
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    server = grpc.aio.server()
    engine_pb2_grpc.add_EngineWorkerServicer_to_server(StreamingWorker(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            async with asyncio.timeout(5):
                await namespace["probe"](
                    engine_pb2_grpc.EngineWorkerStub(channel),
                    (),
                    [engine_pb2.PresetVoice(id="fixture-voice")],
                )
                await cancelled.wait()
    finally:
        await server.stop(0)
