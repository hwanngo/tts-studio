from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.config import Settings
from tts_studio.events import EventStore, UnsafeEventPayloadError
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.models.service import ModelService
from tts_studio.server.app import create_app
from tts_studio.server.events import iter_sse_events
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor


class IdleSupervisor:
    async def statuses(self) -> tuple[object, ...]:
        return ()

    async def stop_all(self) -> None:
        pass


class EventSupervisor:
    def __init__(self, layout: StorageLayout, *, include_total: bool = True) -> None:
        self.layout = layout
        self.include_total = include_total
        self.block_download = False
        self.download_started = asyncio.Event()

    async def validate_model(
        self,
        engine_id: str,
        request: engine_pb2.ValidateModelRequest,
        *,
        timeout: float = 10.0,
    ) -> engine_pb2.ValidateModelResponse:
        del timeout
        return engine_pb2.ValidateModelResponse(
            repository_id=request.repository_id,
            requested_revision=request.requested_revision,
            resolved_commit="a" * 40,
            compatible=True,
            engine_id=engine_id,
            engine_version="1.0.0",
            required_files=["config.json", "model.bin"],
            available_variants=[engine_pb2.ModelVariant(id="int8", label="INT8")],
            estimated_bytes=11 if self.include_total else None,
            evidence=[
                engine_pb2.CompatibilityEvidence(
                    code="fixture_compatible", message="Fixture is compatible"
                )
            ],
        )

    async def download_model(
        self,
        engine_id: str,
        request: engine_pb2.DownloadModelRequest,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[engine_pb2.DownloadModelEvent]:
        del engine_id, timeout
        progress = engine_pb2.DownloadProgress(
            sequence=1,
            phase=engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
            bytes_downloaded=0,
            message="private Worker text at /Users/person/model.bin",
        )
        if self.include_total:
            progress.total_bytes = 11
        self.download_started.set()
        yield engine_pb2.DownloadModelEvent(progress=progress)
        if self.block_download:
            await asyncio.Event().wait()
        files = {"config.json": b"config", "model.bin": b"model"}
        destination = self.layout.staging / request.staging_destination
        for name, content in files.items():
            (destination / name).write_bytes(content)
        verifying = engine_pb2.DownloadProgress(
            sequence=2,
            phase=engine_pb2.DOWNLOAD_PHASE_VERIFYING,
            bytes_downloaded=11,
            message="Verifying",
        )
        if self.include_total:
            verifying.total_bytes = 11
        yield engine_pb2.DownloadModelEvent(progress=verifying)
        yield engine_pb2.DownloadModelEvent(
            manifest=engine_pb2.ModelManifest(
                repository_id=request.repository_id,
                resolved_commit=request.resolved_commit,
                variant=request.variant,
                byte_size=11,
                files=[
                    engine_pb2.ManifestFile(
                        relative_path=name,
                        byte_size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                    for name, content in files.items()
                ],
            )
        )

    async def unload_model(self, engine_id: str, model_id: str, cache_path: Path) -> None:
        del engine_id, model_id, cache_path


def _event_service(
    tmp_path: Path, *, include_total: bool = True
) -> tuple[ModelService, EventStore, EventSupervisor]:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = ModelRegistry(database)
    store = EventStore(database)
    supervisor = EventSupervisor(layout, include_total=include_total)
    descriptor = AdapterDescriptor(
        engine_id="fake",
        priority=10,
        launch=WorkerLaunchSpec(command=("fake-worker",), cwd=tmp_path),
    )
    service = ModelService(
        registry,
        supervisor,
        (descriptor,),
        layout=layout,
        event_store=store,
    )
    return service, store, supervisor


def _store(tmp_path: Path, *, retention_limit: int = 100) -> EventStore:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return EventStore(database, retention_limit=retention_limit)


def _decode_frame(frame: str) -> tuple[int, str, dict[str, object]]:
    fields = {}
    for line in frame.rstrip().splitlines():
        key, value = line.split(": ", maxsplit=1)
        fields[key] = value
    return int(fields["id"]), fields["event"], json.loads(fields["data"])


def test_event_store_assigns_monotonic_ids_and_bounds_retention(tmp_path: Path) -> None:
    # Catches reused IDs and an event table that grows without the configured bound.
    store = _store(tmp_path, retention_limit=3)

    ids = [
        store.append("download.progress", {"job_id": "job-1", "bytes_downloaded": n}).id
        for n in range(5)
    ]

    assert ids == [1, 2, 3, 4, 5]
    batch = store.read_after(0)
    assert batch.cursor_expired is True
    assert batch.reset_cursor == 5
    assert [event.id for event in store.read_after(2).events] == [3, 4, 5]


def test_progress_payload_distinguishes_known_and_unknown_totals(tmp_path: Path) -> None:
    # Catches fabricated totals/percentages for adapters that cannot report a size.
    store = _store(tmp_path)

    unknown = store.append_progress(
        job_id="job-unknown",
        phase="downloading",
        bytes_downloaded=64,
        total_bytes=None,
        message="Downloading model files.",
    )
    known = store.append_progress(
        job_id="job-known",
        phase="downloading",
        bytes_downloaded=25,
        total_bytes=100,
        message="Downloading model files.",
    )

    assert unknown.public_data() == {
        "sequence_id": unknown.id,
        "job_id": "job-unknown",
        "phase": "downloading",
        "bytes_downloaded": 64,
        "message": "Downloading model files.",
    }
    assert known.public_data() == {
        "sequence_id": known.id,
        "job_id": "job-known",
        "phase": "downloading",
        "bytes_downloaded": 25,
        "total_bytes": 100,
        "percentage": 25.0,
        "message": "Downloading model files.",
    }


def test_persisted_payload_cannot_override_the_durable_sequence_id(tmp_path: Path) -> None:
    # Catches client-supplied payload data breaking Last-Event-ID ordering semantics.
    store = _store(tmp_path)

    event = store.append(
        "download.progress",
        {"sequence_id": 999, "job_id": "job-one", "phase": "downloading"},
    )

    assert event.id == 1
    assert event.public_data()["sequence_id"] == 1


def test_event_download_filter_cannot_disagree_with_payload_job_id(tmp_path: Path) -> None:
    # Catches events appearing in one job stream while claiming to belong to another.
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append(
            "download.progress",
            {"job_id": "job-one", "phase": "downloading"},
            download_id="job-two",
        )


def test_typed_generation_stream_identity_preserves_download_compatibility(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    generation = store.append(
        "generation.progress",
        {"job_id": "generation-one", "bytes_written": 4},
        stream_kind="generation",
        stream_id="generation-one",
    )
    download = store.append(
        "download.progress",
        {"job_id": "download-one", "bytes_downloaded": 4},
        download_id="download-one",
    )

    assert (generation.stream_kind, generation.stream_id) == ("generation", "generation-one")
    assert generation.download_id is None
    assert (download.stream_kind, download.stream_id) == ("download", "download-one")
    assert download.download_id == "download-one"
    assert [event.id for event in store.read_after(0, stream_kind="generation", stream_id="generation-one").events] == [generation.id]
    assert [event.id for event in store.read_after(0, download_id="download-one").events] == [download.id]


def test_generation_event_payload_never_accepts_audio_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append(
            "generation.progress",
            {"job_id": "generation-one", "audio_bytes": "AAECAw=="},
            stream_kind="generation",
            stream_id="generation-one",
        )


def test_generation_events_require_explicit_typed_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append("generation.queued", {"job_id": "generation-one"})


def test_generation_audio_keys_are_rejected_without_typed_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append("generation.progress", {"job_id": "generation-one", "pcm": "AAE="})


@pytest.mark.parametrize(
    "payload",
    [
        {"metadata": {"pcm": "AAE="}},
        {"metadata": [{"audio_bytes": "AAE="}]},
        {"metadata": {"nested": {"wav": "UklGRg=="}}},
    ],
)
def test_generation_event_rejects_nested_audio_metadata(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append(
            "generation.progress",
            payload,
            stream_kind="generation",
            stream_id="generation-one",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stream_kind": "generation"},
        {"stream_id": "generation-one"},
    ],
)
def test_event_store_rejects_partial_stream_identity_on_append(
    tmp_path: Path, kwargs: dict[str, str]
) -> None:
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append("model.validation", {}, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stream_kind": "generation"},
        {"stream_id": "generation-one"},
    ],
)
def test_event_store_rejects_partial_stream_identity_on_read(
    tmp_path: Path, kwargs: dict[str, str]
) -> None:
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.read_after(0, **kwargs)


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "failed at /Users/person/private/model.bin"},
        {"message": r"failed at C:\\Users\\person\\private\\model.bin"},
        {"token": "hf_secret_value"},
        {"nested": {"authorization": "Bearer private"}},
        {"message": f"credential hf_{'a' * 32}"},
    ],
)
def test_event_store_rejects_secret_and_absolute_path_payloads(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    # Catches persistence that would later leak sensitive Worker details over SSE.
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append("download.failed", payload)

    assert store.read_after(0).events == ()


@pytest.mark.parametrize(
    "unsafe_message",
    [
        "download failed (/Users/person/private/model.bin)",
        "download failed (file:///Users/person/private/model.bin)",
        r"download failed (C:\Users\person\private\model.bin)",
        r"download failed (\\server\private\model.bin)",
        "download failed (//server/share/private.bin)",
    ],
)
def test_event_store_rejects_machine_paths_embedded_after_punctuation(
    tmp_path: Path, unsafe_message: str
) -> None:
    # Catches delimiters other than whitespace bypassing event path sanitization.
    store = _store(tmp_path)

    with pytest.raises(UnsafeEventPayloadError):
        store.append("download.failed", {"message": unsafe_message})


def test_event_store_allows_valid_hugging_face_ids_that_resemble_token_prefixes(
    tmp_path: Path,
) -> None:
    # Catches broad hf_ token matching that rejects a legitimate repository component.
    store = _store(tmp_path)

    event = store.append(
        "model.validation",
        {"repository_id": "owner/hf_abcdefgh", "compatible": True},
    )

    assert event.public_data()["repository_id"] == "owner/hf_abcdefgh"


def test_event_store_does_not_treat_https_urls_as_forward_slash_unc_paths(
    tmp_path: Path,
) -> None:
    # Catches privacy filtering that also suppresses safe adapter documentation links.
    store = _store(tmp_path)

    event = store.append(
        "model.validation",
        {"message": "See https://huggingface.co/owner/model for documentation."},
    )

    assert event.public_data()["message"] == (
        "See https://huggingface.co/owner/model for documentation."
    )


class _InterleavingConnection:
    def __init__(self, connection: sqlite3.Connection, interleave: Any) -> None:
        self._connection = connection
        self._interleave = interleave
        self._did_interleave = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()):
        result = self._connection.execute(sql, parameters)
        if not self._did_interleave and "SELECT last_event_id" in sql:
            self._did_interleave = True
            self._interleave()
        return result


class _InterleavingDatabase(Database):
    def __init__(self, path: Path, interleave: Any) -> None:
        super().__init__(path)
        self._interleave = interleave

    @contextmanager
    def read(self):
        with super().read() as connection:
            yield _InterleavingConnection(connection, self._interleave)


def test_read_after_uses_one_snapshot_when_retention_prunes_concurrently(tmp_path: Path) -> None:
    # Catches latest/oldest/event queries observing different SQLite snapshots.
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    with sqlite3.connect(layout.database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    writer = EventStore(database, retention_limit=2)
    writer.append("download.progress", {"job_id": "job-one", "bytes_downloaded": 1})
    writer.append("download.progress", {"job_id": "job-one", "bytes_downloaded": 2})

    def prune_old_snapshot() -> None:
        writer.append("download.progress", {"job_id": "job-one", "bytes_downloaded": 3})
        writer.append("download.progress", {"job_id": "job-one", "bytes_downloaded": 4})

    reader = EventStore(
        _InterleavingDatabase(layout.database_path, prune_old_snapshot),
        retention_limit=2,
    )

    batch = reader.read_after(0)

    assert batch.cursor_expired is False
    assert [event.id for event in batch.events] == [1, 2]
    assert [event.id for event in writer.read_after(2).events] == [3, 4]


@pytest.mark.asyncio
async def test_sse_reconnect_resumes_exclusively_after_last_event_id(tmp_path: Path) -> None:
    # Catches at-least-once replay of an already rendered activation event.
    store = _store(tmp_path)
    first = store.append("download.progress", {"job_id": "job-1", "phase": "downloading"})
    second = store.append("download.progress", {"job_id": "job-1", "phase": "verifying"})
    terminal = store.append(
        "model.activated",
        {"job_id": "job-1", "model_id": "model-1", "phase": "completed"},
    )

    frames = [
        frame
        async for frame in iter_sse_events(
            store,
            last_event_id=first.id,
            download_id="job-1",
            poll_interval=0.001,
        )
    ]

    assert [_decode_frame(frame)[0] for frame in frames] == [second.id, terminal.id]
    assert [_decode_frame(frame)[1] for frame in frames] == [
        "download.progress",
        "model.activated",
    ]

    async def disconnected() -> bool:
        return True

    duplicate_frames = [
        frame
        async for frame in iter_sse_events(
            store,
            last_event_id=terminal.id,
            download_id="job-1",
            is_disconnected=disconnected,
            poll_interval=0.001,
        )
    ]
    assert duplicate_frames == []


@pytest.mark.asyncio
async def test_expired_sse_cursor_emits_reset_and_closes(tmp_path: Path) -> None:
    # Catches silently skipping retained history after a client falls behind retention.
    store = _store(tmp_path, retention_limit=2)
    for number in range(4):
        store.append(
            "download.progress",
            {"job_id": "job-1", "phase": "downloading", "bytes_downloaded": number},
        )

    frames = [
        frame
        async for frame in iter_sse_events(
            store,
            last_event_id=1,
            download_id="job-1",
            poll_interval=0.001,
        )
    ]

    assert len(frames) == 1
    event_id, event_type, data = _decode_frame(frames[0])
    assert event_id == 4
    assert event_type == "stream.reset"
    assert data == {
        "sequence_id": 4,
        "reason": "retention_expired",
        "message": "Event history expired; refresh the current model and download state.",
    }


@pytest.mark.asyncio
async def test_slow_consumer_uses_durable_retention_instead_of_an_unbounded_queue(
    tmp_path: Path,
) -> None:
    # Catches per-client queues that grow while a browser is paused or disconnected.
    store = _store(tmp_path, retention_limit=3)
    store.append("download.progress", {"job_id": "job-1", "bytes_downloaded": 0})
    stream = iter_sse_events(
        store,
        last_event_id=None,
        download_id="job-1",
        batch_size=1,
        poll_interval=0.001,
    )
    first_frame = await anext(stream)
    assert _decode_frame(first_frame)[0] == 1

    for number in range(1, 101):
        store.append(
            "download.progress",
            {"job_id": "job-1", "bytes_downloaded": number},
        )

    assert store.retained_count() == 3
    reset_frame = await anext(stream)
    assert _decode_frame(reset_frame)[1] == "stream.reset"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_type", ["download.cancelled", "download.failed"])
async def test_download_scoped_stream_closes_after_terminal_event(
    tmp_path: Path, terminal_type: str
) -> None:
    # Catches filtered streams that hang after a durable terminal job outcome.
    store = _store(tmp_path)
    terminal = store.append(
        terminal_type,
        {
            "job_id": "job-terminal",
            "phase": terminal_type.removeprefix("download."),
            "message": "The download reached a terminal state.",
        },
    )

    frames = [
        frame
        async for frame in iter_sse_events(
            store,
            last_event_id=None,
            download_id="job-terminal",
            poll_interval=0.001,
        )
    ]

    assert [_decode_frame(frame)[0] for frame in frames] == [terminal.id]


@pytest.mark.asyncio
async def test_http_events_route_honors_last_event_id_and_safe_sse_headers(
    tmp_path: Path,
) -> None:
    # Catches a route wired to transient state or ignoring the browser resume cursor.
    data_dir = tmp_path / "data"
    app = create_app(
        Settings.resolve(data_dir),
        supervisor=cast(WorkerSupervisor, IdleSupervisor()),
        adapters=(),
    )

    async with app.router.lifespan_context(app):
        store = app.state.event_store
        first = store.append(
            "download.progress", {"job_id": "job-http", "phase": "downloading"}
        )
        second = store.append(
            "download.cancelled",
            {
                "job_id": "job-http",
                "phase": "cancelled",
                "message": "Model download was cancelled.",
            },
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/events",
                params={"download_id": "job-http"},
                headers={"Last-Event-ID": str(first.id)},
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert f"id: {first.id}\n" not in response.text
    assert f"id: {second.id}\n" in response.text
    assert "event: download.cancelled\n" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("include_total", [True, False])
async def test_model_service_publishes_validation_progress_and_activation_checkpoints(
    tmp_path: Path, include_total: bool
) -> None:
    # Catches SSE that is durable in isolation but disconnected from real Download Jobs.
    service, store, _ = _event_service(tmp_path, include_total=include_total)

    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    completed = await service.wait_for_download(queued.id)

    assert completed.state is DownloadState.COMPLETED
    events = store.read_after(0).events
    types = [event.event_type for event in events]
    assert "model.validation" in types
    assert "download.queued" in types
    assert types[-1] == "model.activated"
    progress = [event.public_data() for event in events if event.event_type == "download.progress"]
    assert progress
    assert progress[0]["bytes_downloaded"] == 0
    if include_total:
        known = next(event for event in progress if "total_bytes" in event)
        assert known["total_bytes"] == 11
        assert known["percentage"] == 0.0
    else:
        assert all("total_bytes" not in event for event in progress)
        assert all("percentage" not in event for event in progress)
    encoded = json.dumps([event.public_data() for event in events])
    assert str(tmp_path) not in encoded
    assert "/Users/person" not in encoded


@pytest.mark.asyncio
async def test_model_service_publishes_one_terminal_cancellation_event(tmp_path: Path) -> None:
    # Catches cancellation state that is durable in jobs but invisible or duplicated in SSE.
    service, store, supervisor = _event_service(tmp_path)
    supervisor.block_download = True
    queued = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    await asyncio.wait_for(supervisor.download_started.wait(), timeout=1)

    await service.cancel_download(queued.id)
    cancelled = await service.wait_for_download(queued.id)
    await service.cancel_download(queued.id)

    assert cancelled.state is DownloadState.CANCELLED
    terminal = [
        event
        for event in store.read_after(0).events
        if event.event_type == "download.cancelled"
    ]
    assert len(terminal) == 1
    assert terminal[0].public_data()["job_id"] == queued.id


@pytest.mark.asyncio
async def test_activation_event_failure_never_rolls_back_a_committed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches event persistence failure leaving model metadata pointed at rolled-back files.
    service, store, _ = _event_service(tmp_path)
    original_append = store.append

    def fail_activation_event(
        event_type: str,
        payload: dict[str, object],
        *,
        download_id: str | None = None,
    ):
        if event_type == "model.activated":
            raise RuntimeError("event database unavailable")
        return original_append(event_type, payload, download_id=download_id)

    monkeypatch.setattr(store, "append", fail_activation_event)

    job = await service.start_download(
        "fixtures/compatible", variant="int8", correlation_id="correlation-one"
    )
    completed = await service.wait_for_download(job.id)

    model = service.list_models()[0]
    assert completed.state is DownloadState.COMPLETED
    assert (tmp_path / "data" / model.cache_path).is_dir()
