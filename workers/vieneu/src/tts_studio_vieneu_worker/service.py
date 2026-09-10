"""Authenticated, lightweight VieNeu Worker RPC service."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

import grpc
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_worker_sdk.auth import require_worker_token
from tts_studio_worker_sdk.limits import MAX_SYNTHESIS_TEXT_CHARS

from tts_studio_vieneu_worker.constants import (
    CAPABILITIES,
    ENGINE_ID,
    ENGINE_VERSION,
    MAX_CONCURRENCY,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
)
from tts_studio_vieneu_worker.huggingface import RepositoryClient
from tts_studio_vieneu_worker.model_service import VieNeuModelService
from tts_studio_vieneu_worker.recovery import recover_download_scratch
from tts_studio_vieneu_worker.runtime import RuntimeFailure, VieNeuRuntime

_SYNTHESIS_QUIESCE_TIMEOUT_SECONDS = 0.25


class VieNeuEngineWorker(engine_pb2_grpc.EngineWorkerServicer):
    """The startup shell for the isolated VieNeu adapter.

    SDK import and model construction intentionally stay out of this class's
    constructor and health path. Model acquisition is delegated to the
    injected repository-backed model service; lifecycle remains deferred.
    """

    def __init__(
        self,
        token: str,
        data_root: Path,
        repository: RepositoryClient | None = None,
        vieneu_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._token = token
        self._data_root = data_root
        recover_download_scratch(data_root)
        self._model_service = VieNeuModelService(repository, data_root)
        self._runtime = VieNeuRuntime(data_root, vieneu_factory)

    async def Describe(
        self,
        request: engine_pb2.DescribeRequest,
        context: grpc.aio.ServicerContext[engine_pb2.DescribeRequest, engine_pb2.DescribeResponse],
    ) -> engine_pb2.DescribeResponse:
        await require_worker_token(context, self._token)
        capabilities = tuple(
            name
            for name in CAPABILITIES
            if name != "reference_cloning" or self._runtime.reference_cloning_supported()
        )
        return engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=PROTOCOL_MAJOR, minor=PROTOCOL_MINOR),
            engine_id=ENGINE_ID,
            engine_version=ENGINE_VERSION,
            capabilities=[engine_pb2.Capability(name=name, supported=True) for name in capabilities],
            max_concurrency=MAX_CONCURRENCY,
        )

    async def Health(
        self,
        request: engine_pb2.HealthRequest,
        context: grpc.aio.ServicerContext[engine_pb2.HealthRequest, engine_pb2.HealthResponse],
    ) -> engine_pb2.HealthResponse:
        await require_worker_token(context, self._token)
        return engine_pb2.HealthResponse(status=engine_pb2.HealthResponse.READY)

    async def ValidateModel(
        self,
        request: engine_pb2.ValidateModelRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.ValidateModelRequest, engine_pb2.ValidateModelResponse
        ],
    ) -> engine_pb2.ValidateModelResponse:
        await require_worker_token(context, self._token)
        return self._model_service.validate(request)

    async def DownloadModel(
        self,
        request: engine_pb2.DownloadModelRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.DownloadModelRequest, engine_pb2.DownloadModelEvent
        ],
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        await require_worker_token(context, self._token)
        async for event in self._model_service.download(request, context):
            yield event

    async def LoadModel(
        self,
        request: engine_pb2.LoadModelRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.LoadModelRequest, engine_pb2.LoadModelResponse
        ],
    ) -> engine_pb2.LoadModelResponse:
        await require_worker_token(context, self._token)
        try:
            self._runtime.load(request.model_id, request.cache_path, request.variant)
        except RuntimeFailure as error:
            return engine_pb2.LoadModelResponse(error=_runtime_error(error))
        return engine_pb2.LoadModelResponse(loaded=True)

    async def UnloadModel(
        self,
        request: engine_pb2.UnloadModelRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.UnloadModelRequest, engine_pb2.UnloadModelResponse
        ],
    ) -> engine_pb2.UnloadModelResponse:
        await require_worker_token(context, self._token)
        try:
            self._runtime.unload(request.model_id)
        except RuntimeFailure as error:
            return engine_pb2.UnloadModelResponse(error=_runtime_error(error))
        return engine_pb2.UnloadModelResponse(unloaded=True)

    async def ListVoices(
        self,
        request: engine_pb2.ListVoicesRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.ListVoicesRequest, engine_pb2.ListVoicesResponse
        ],
    ) -> engine_pb2.ListVoicesResponse:
        await require_worker_token(context, self._token)
        try:
            voices = self._runtime.list_voices(request.model_id)
        except RuntimeFailure as error:
            return engine_pb2.ListVoicesResponse(error=_runtime_error(error))
        return engine_pb2.ListVoicesResponse(voices=voices)

    async def ValidateReference(
        self,
        request: engine_pb2.ValidateReferenceRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.ValidateReferenceRequest, engine_pb2.ValidateReferenceResponse
        ],
    ) -> engine_pb2.ValidateReferenceResponse:
        await require_worker_token(context, self._token)
        try:
            metadata = self._runtime.validate_reference(
                request.model_id,
                request.reference_path,
                request.transcript if request.HasField("transcript") else None,
            )
        except RuntimeFailure as error:
            return engine_pb2.ValidateReferenceResponse(error=_runtime_error(error))
        return engine_pb2.ValidateReferenceResponse(valid=True, metadata=metadata)

    async def Align(
        self,
        request: engine_pb2.AlignRequest,
        context: grpc.aio.ServicerContext[engine_pb2.AlignRequest, engine_pb2.AlignResponse],
    ) -> engine_pb2.AlignResponse:
        await require_worker_token(context, self._token)
        try:
            self._runtime.align(request.model_id, request.audio_path, request.transcript)
        except RuntimeFailure as error:
            return engine_pb2.AlignResponse(
                error=engine_pb2.WorkerError(
                    code=error.code,
                    message=error.message,
                    retryable=False,
                    details={"adapter": "vieneu", "operation": "alignment"},
                )
            )
        raise AssertionError("VieNeu alignment unexpectedly returned without a result")

    async def Synthesize(
        self,
        request: engine_pb2.SynthesizeRequest,
        context: grpc.aio.ServicerContext[
            engine_pb2.SynthesizeRequest, engine_pb2.SynthesisEvent
        ],
    ) -> AsyncIterator[engine_pb2.SynthesisEvent]:
        await require_worker_token(context, self._token)
        try:
            if request.HasField("provider"):
                raise RuntimeFailure("provider_unsupported", "Provider configuration is unsupported by VieNeu")
            if request.HasField("options"):
                for field in ("speed", "pitch", "volume"):
                    if request.options.HasField(field):
                        raise RuntimeFailure("option_unsupported", f"The {field} option is unsupported by VieNeu")
            if len(request.text) > MAX_SYNTHESIS_TEXT_CHARS:
                raise RuntimeFailure("input_too_large", "The synthesis input is too large")
            source = request.WhichOneof("voice_source")
            if source == "voice_id":
                self._runtime.validate_voice(request.model_id, request.voice_id)
            elif source == "reference":
                self._runtime.validate_reference(
                    request.model_id,
                    request.reference.reference_path,
                    request.reference.transcript if request.reference.HasField("transcript") else None,
                )
            else:
                self._runtime.engine_for(request.model_id)
                raise RuntimeFailure("invalid_request", "A preset voice or reference is required")
        except RuntimeFailure as error:
            yield engine_pb2.SynthesisEvent(error=_runtime_error(error))
            return

        cancellation = threading.Event()
        loop = asyncio.get_running_loop()
        cancellation_wakeup: asyncio.Future[None] = loop.create_future()

        def wake_cancellation() -> None:
            if not cancellation_wakeup.done():
                cancellation_wakeup.set_result(None)

        add_done_callback = getattr(context, "add_done_callback", None)
        if callable(add_done_callback):
            def on_done(_: object) -> None:
                cancellation.set()
                try:
                    loop.call_soon_threadsafe(wake_cancellation)
                except RuntimeError:
                    # The event loop may already be closing after the RPC returned.
                    pass

            add_done_callback(on_done)
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=1)

        def produce() -> None:
            try:
                reference = request.reference if source == "reference" else None
                for pcm in self._runtime.synthesize(
                    request.model_id,
                    request.voice_id if source == "voice_id" else None,
                    request.text,
                    cancellation,
                    reference_path=reference.reference_path if reference else None,
                    transcript=(
                        reference.transcript
                        if reference is not None and reference.HasField("transcript")
                        else None
                    ),
                ):
                    if not _put_from_thread(loop, queue, ("chunk", pcm), cancellation):
                        return
                _put_from_thread(loop, queue, ("done", None), cancellation)
            except RuntimeFailure as error:
                _put_from_thread(loop, queue, ("error", error), cancellation)
            except Exception:  # noqa: BLE001 - keep raw SDK failures behind WorkerError
                _put_from_thread(
                    loop,
                    queue,
                    ("error", RuntimeFailure("synthesis_failed", "VieNeu synthesis failed")),
                    cancellation,
                )

        thread = threading.Thread(target=produce, name="vieneu-synthesis", daemon=True)
        thread.start()
        completed = False
        total_frames = 0
        sequence = 0
        try:
            first_kind, first_value = await _next_item(queue, cancellation_wakeup)
            if first_kind == "cancelled":
                yield engine_pb2.SynthesisEvent(error=_termination_error(context) or _cancelled_error())
                return
            if first_kind == "error":
                yield engine_pb2.SynthesisEvent(error=_runtime_error(first_value))
                return
            if first_kind == "done":
                yield engine_pb2.SynthesisEvent(
                    error=_runtime_error(RuntimeFailure("invalid_audio", "VieNeu returned no audio"))
                )
                return
            yield engine_pb2.SynthesisEvent(
                header=engine_pb2.AudioHeader(
                    sample_rate_hz=48000, channels=1, sample_format=engine_pb2.S16LE
                )
            )
            pending: tuple[str, object] | None = (first_kind, first_value)
            while pending is not None:
                termination = _termination_error(context)
                if termination is not None:
                    cancellation.set()
                    yield engine_pb2.SynthesisEvent(error=termination)
                    return
                kind, value = pending
                if kind == "chunk":
                    pcm = value
                    if not isinstance(pcm, bytes) or len(pcm) % 2:
                        yield engine_pb2.SynthesisEvent(
                            error=_runtime_error(
                                RuntimeFailure("invalid_audio", "VieNeu returned invalid audio")
                            )
                        )
                        return
                    yield engine_pb2.SynthesisEvent(
                        chunk=engine_pb2.PcmChunk(sequence=sequence, pcm=pcm)
                    )
                    sequence += 1
                    total_frames += len(pcm) // 2
                elif kind == "error":
                    yield engine_pb2.SynthesisEvent(error=_runtime_error(value))
                    return
                elif kind == "done":
                    if cancellation.is_set():
                        termination = _termination_error(context) or _cancelled_error()
                        yield engine_pb2.SynthesisEvent(error=termination)
                        return
                    if total_frames == 0:
                        yield engine_pb2.SynthesisEvent(
                            error=_runtime_error(
                                RuntimeFailure("invalid_audio", "VieNeu returned no audio")
                            )
                        )
                        return
                    yield engine_pb2.SynthesisEvent(
                        progress=engine_pb2.SynthesisProgress(
                            message="VieNeu synthesis complete", duration_frames=total_frames
                        )
                    )
                    yield engine_pb2.SynthesisEvent(
                        result=engine_pb2.SynthesisResult(
                            total_frames=total_frames, duration_ms=round(total_frames / 48)
                        )
                    )
                    completed = True
                    return
                pending = await _next_item(queue, cancellation_wakeup)
                if pending[0] == "cancelled":
                    yield engine_pb2.SynthesisEvent(
                        error=_termination_error(context) or _cancelled_error()
                    )
                    return
        finally:
            if not completed:
                cancellation.set()
            # Most SDK calls observe cancellation promptly.  Bound the join so
            # a native call that ignores cancellation cannot hold the RPC open
            # forever; the runtime is quarantined if inference is still alive,
            # and lifecycle cleanup then fails closed instead of claiming safety.
            await asyncio.to_thread(thread.join, _SYNTHESIS_QUIESCE_TIMEOUT_SECONDS)
            if thread.is_alive():
                self._runtime.quarantine_synthesis()


def _put_from_thread(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[tuple[str, object]],
    item: tuple[str, object],
    cancellation: threading.Event,
) -> bool:
    while not cancellation.is_set():
        try:
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        except RuntimeError:
            return False
        try:
            future.result(timeout=0.05)
            return True
        except FutureTimeoutError:
            future.cancel()
    return False


async def _next_item(
    queue: asyncio.Queue[tuple[str, object]], cancellation_wakeup: asyncio.Future[None]
) -> tuple[str, object]:
    async def wait_for_wakeup() -> None:
        await asyncio.shield(cancellation_wakeup)

    queue_task = asyncio.create_task(queue.get())
    wake_task = asyncio.create_task(wait_for_wakeup())
    try:
        done, _ = await asyncio.wait({queue_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
        if wake_task in done:
            return ("cancelled", None)
        return queue_task.result()
    finally:
        for task in (queue_task, wake_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(queue_task, wake_task, return_exceptions=True)


def _termination_error(context: grpc.aio.ServicerContext) -> engine_pb2.WorkerError | None:
    time_remaining = getattr(context, "time_remaining", None)
    remaining = time_remaining() if callable(time_remaining) else None
    if remaining is not None and remaining <= 0.005:
        return engine_pb2.WorkerError(
            code="synthesis_deadline_exceeded",
            message="Synthesis deadline exceeded",
            retryable=True,
            details={"adapter": "vieneu", "terminal_status": "DEADLINE_EXCEEDED"},
        )
    cancelled = getattr(context, "cancelled", None)
    if callable(cancelled) and cancelled():
        return _cancelled_error()
    return None


def _cancelled_error() -> engine_pb2.WorkerError:
    return engine_pb2.WorkerError(
        code="synthesis_cancelled",
        message="Synthesis was cancelled",
        retryable=False,
        details={"adapter": "vieneu", "terminal_status": "CANCELLED"},
    )


def _runtime_error(error: RuntimeFailure) -> engine_pb2.WorkerError:
    return engine_pb2.WorkerError(code=error.code, message=error.message, retryable=False)
