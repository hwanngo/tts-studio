"""Test-only fake adapter with a native call and helper that ignore cancellation."""

import asyncio
import sys
import threading
import time

from tts_studio_fake_worker.main import _parse_args
from tts_studio_fake_worker.service import FakeEngineWorker
from tts_studio_protocol.engine.v1 import engine_pb2
from tts_studio_worker_sdk.auth import consume_worker_token, require_worker_token
from tts_studio_worker_sdk.server import serve_worker


class StuckWorker(FakeEngineWorker):
    stuck = False

    async def Synthesize(self, request, context):
        await require_worker_token(context, self._token)
        marker = args.data_dir / "staging" / "stuck-once"
        if marker.exists():
            async for event in super().Synthesize(request, context):
                yield event
            return
        marker.touch()
        self.stuck = True
        threading.Thread(target=lambda: time.sleep(60), daemon=True).start()
        helper = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)",
        )
        (args.data_dir / "staging" / "stuck-helper.pid").write_text(str(helper.pid))
        if request.text == "retry":
            yield engine_pb2.SynthesisEvent(
                error=engine_pb2.WorkerError(
                    code="provider_unavailable",
                    message="transient fixture failure",
                    retryable=True,
                )
            )
            return
        yield engine_pb2.SynthesisEvent(
            header=engine_pb2.AudioHeader(
                sample_rate_hz=48000,
                channels=1,
                sample_format=engine_pb2.S16LE,
            )
        )
        await asyncio.Event().wait()

    async def UnloadModel(self, request, context):
        await require_worker_token(context, self._token)
        if self.stuck:
            return engine_pb2.UnloadModelResponse(
                error=engine_pb2.WorkerError(
                    code="model_unload_failed",
                    message="native fixture call is still active",
                )
            )
        return await super().UnloadModel(request, context)


args = _parse_args()
token = consume_worker_token(args.token_file)
asyncio.run(
    serve_worker(
        StuckWorker(token, args.data_dir / "staging"),
        host=args.host,
        port=args.port,
        token=token,
        ready_file=args.ready_file,
    )
)
