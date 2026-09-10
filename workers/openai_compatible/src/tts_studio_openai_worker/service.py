from __future__ import annotations

from collections.abc import AsyncIterator

from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_worker_sdk.auth import require_worker_token
from tts_studio_worker_sdk.limits import MAX_SYNTHESIS_TEXT_CHARS

from tts_studio_openai_worker.http import ProviderCancellation, ProviderRequestError, synthesize


class OpenAICompatibleWorker(engine_pb2_grpc.EngineWorkerServicer):
    def __init__(self, token: str) -> None:
        self._token = token
        self._provider_quarantined = False

    async def Describe(self, request, context) -> engine_pb2.DescribeResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=1, minor=1),
            engine_id="openai_compatible", engine_version="0.1.0",
            capabilities=[
                engine_pb2.Capability(name="health", supported=True),
                engine_pb2.Capability(name="preset_voices", supported=True),
                engine_pb2.Capability(name="streaming_synthesis", supported=True),
                engine_pb2.Capability(name="synthesis_cancellation", supported=True),
                engine_pb2.Capability(name="speed", supported=True),
            ], max_concurrency=1,
        )

    async def Health(self, request, context) -> engine_pb2.HealthResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.HealthResponse(
            status=(
                engine_pb2.HealthResponse.DEGRADED
                if self._provider_quarantined
                else engine_pb2.HealthResponse.READY
            )
        )

    async def LoadModel(self, request, context) -> engine_pb2.LoadModelResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.LoadModelResponse(loaded=True)

    async def UnloadModel(self, request, context) -> engine_pb2.UnloadModelResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.UnloadModelResponse(unloaded=True)

    async def ListVoices(self, request, context) -> engine_pb2.ListVoicesResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.ListVoicesResponse(voices=[
            engine_pb2.PresetVoice(id=value, label=value.title())
            for value in ("alloy", "echo", "fable", "onyx", "nova", "shimmer")
        ])

    async def Synthesize(self, request, context) -> AsyncIterator[engine_pb2.SynthesisEvent]:
        await require_worker_token(context, self._token)
        if self._provider_quarantined:
            yield _error(
                "worker_quarantined",
                "The provider Worker requires replacement after an unbounded operation.",
                True,
            )
            return
        if len(request.text) > MAX_SYNTHESIS_TEXT_CHARS:
            yield _error("input_too_large", "The synthesis input is too large.", False)
            return
        if request.WhichOneof("voice_source") != "voice_id":
            yield _error("voice_unsupported", "A preset provider voice is required.", False)
            return
        if request.HasField("options"):
            for field in ("pitch", "volume"):
                if request.options.HasField(field):
                    yield _error("option_unsupported", f"The {field} option is unsupported.", False)
                    return
        if not request.HasField("provider") or not request.provider.api_key:
            yield _error("provider_configuration_missing", "The provider credential is not configured.", False)
            return
        cancellation = ProviderCancellation()
        add_done_callback = getattr(context, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(lambda _: cancellation.cancel())
        if getattr(context, "cancelled", lambda: False)():
            cancellation.cancel()
        try:
            audio = await synthesize(
                base_url=request.provider.base_url, api_key=request.provider.api_key,
                model=request.provider.model, text=request.text, voice=request.voice_id,
                speed=(request.options.speed if request.HasField("options") and request.options.HasField("speed") else None),
                cancellation=cancellation,
            )
        except ProviderRequestError as error:
            if cancellation.quarantined():
                self._provider_quarantined = True
            yield _error(error.code, str(error), error.retryable)
            return
        finally:
            if cancellation.quarantined():
                self._provider_quarantined = True
        yield engine_pb2.SynthesisEvent(header=engine_pb2.AudioHeader(
            sample_rate_hz=audio.sample_rate_hz, channels=1, sample_format=engine_pb2.S16LE,
        ))
        for sequence, offset in enumerate(range(0, len(audio.pcm), 1920)):
            if context.cancelled():
                return
            yield engine_pb2.SynthesisEvent(chunk=engine_pb2.PcmChunk(sequence=sequence, pcm=audio.pcm[offset:offset + 1920]))
        yield engine_pb2.SynthesisEvent(result=engine_pb2.SynthesisResult(total_frames=audio.frames, duration_ms=audio.frames * 1000 // 48000))

    async def ValidateModel(self, request, context):
        await require_worker_token(context, self._token)
        return engine_pb2.ValidateModelResponse(compatible=False, engine_id="openai_compatible", engine_version="0.1.0")

    async def ValidateReference(self, request, context):
        await require_worker_token(context, self._token)
        return engine_pb2.ValidateReferenceResponse(valid=False, error=engine_pb2.WorkerError(code="reference_unsupported", message="Reference cloning is not supported.", retryable=False))

    async def DownloadModel(self, request, context):
        await require_worker_token(context, self._token)
        yield engine_pb2.DownloadModelEvent(error=engine_pb2.WorkerError(code="model_download_unsupported", message="Remote providers do not download models.", retryable=False))


def _error(code: str, message: str, retryable: bool) -> engine_pb2.SynthesisEvent:
    return engine_pb2.SynthesisEvent(error=engine_pb2.WorkerError(code=code, message=message, retryable=retryable))
