"""Resumable Server-Sent Events for model-management state."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from tts_studio.events import DurableEvent, EventStore

router = APIRouter(prefix="/api/v1", tags=["events"])

_Disconnected = Callable[[], Awaitable[bool]]


@router.get("/events", response_class=StreamingResponse)
async def model_events(
    request: Request,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
    download_id: Annotated[
        str | None,
        Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ] = None,
) -> StreamingResponse:
    return StreamingResponse(
        iter_sse_events(
            request.app.state.event_store,
            last_event_id=last_event_id,
            download_id=download_id,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def iter_sse_events(
    store: EventStore,
    *,
    last_event_id: int | None,
    download_id: str | None = None,
    is_disconnected: _Disconnected | None = None,
    batch_size: int = 100,
    poll_interval: float = 0.1,
) -> AsyncIterator[str]:
    """Read bounded durable batches so slow clients never accumulate an in-memory queue."""
    cursor = (
        await asyncio.to_thread(store.initial_cursor) if last_event_id is None else last_event_id
    )
    while True:
        batch = await asyncio.to_thread(
            store.read_after,
            cursor,
            download_id=download_id,
            limit=batch_size,
        )
        if batch.cursor_expired:
            yield _encode_sse(
                batch.reset_cursor,
                "stream.reset",
                {
                    "sequence_id": batch.reset_cursor,
                    "reason": "retention_expired",
                    "message": (
                        "Event history expired; refresh the current model and download state."
                    ),
                },
            )
            return
        if batch.events:
            for event in batch.events:
                yield _event_frame(event)
                cursor = event.id
                if download_id is not None and event.terminal:
                    return
            continue
        if is_disconnected is not None and await is_disconnected():
            return
        await asyncio.sleep(poll_interval)


def _event_frame(event: DurableEvent) -> str:
    return _encode_sse(event.id, event.event_type, event.public_data())


def _encode_sse(event_id: int, event_type: str, data: Mapping[str, object]) -> str:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"id: {event_id}\nevent: {event_type}\ndata: {encoded}\n\n"
