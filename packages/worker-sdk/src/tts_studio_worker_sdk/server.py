"""Lifecycle management for private engine worker gRPC servers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import signal
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import grpc
from tts_studio_protocol.engine.v1 import engine_pb2_grpc

from tts_studio_worker_sdk.auth import require_worker_token


class _WorkerTokenInterceptor(grpc.aio.ServerInterceptor):
    def __init__(self, token: str) -> None:
        self._token = token

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails],
            Awaitable[grpc.RpcMethodHandler[Any, Any] | None],
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        handler = await continuation(handler_call_details)
        if handler is None:
            return None

        if handler.unary_unary is not None:
            return grpc.unary_unary_rpc_method_handler(
                self._authenticated_unary_unary(handler.unary_unary),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.unary_stream is not None:
            return grpc.unary_stream_rpc_method_handler(
                self._authenticated_unary_stream(
                    cast(Callable[..., AsyncIterator[object]], handler.unary_stream)
                ),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_unary is not None:
            return grpc.stream_unary_rpc_method_handler(
                self._authenticated_stream_unary(handler.stream_unary),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_stream is not None:
            return grpc.stream_stream_rpc_method_handler(
                self._authenticated_stream_stream(
                    cast(Callable[..., AsyncIterator[object]], handler.stream_stream)
                ),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        return handler

    def _authenticated_unary_unary(self, behavior: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
        async def authenticated(
            request: object, context: grpc.aio.ServicerContext[Any, Any]
        ) -> object:
            await require_worker_token(context, self._token)
            return await behavior(request, context)

        return authenticated

    def _authenticated_unary_stream(
        self, behavior: Callable[..., AsyncIterator[object]]
    ) -> Callable[..., AsyncIterator[object]]:
        async def authenticated(
            request: object, context: grpc.aio.ServicerContext[Any, Any]
        ) -> AsyncIterator[object]:
            await require_worker_token(context, self._token)
            async for response in behavior(request, context):
                yield response

        return authenticated

    def _authenticated_stream_unary(self, behavior: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
        async def authenticated(
            request_iterator: AsyncIterator[object],
            context: grpc.aio.ServicerContext[Any, Any],
        ) -> object:
            await require_worker_token(context, self._token)
            return await behavior(request_iterator, context)

        return authenticated

    def _authenticated_stream_stream(
        self, behavior: Callable[..., AsyncIterator[object]]
    ) -> Callable[..., AsyncIterator[object]]:
        async def authenticated(
            request_iterator: AsyncIterator[object],
            context: grpc.aio.ServicerContext[Any, Any],
        ) -> AsyncIterator[object]:
            await require_worker_token(context, self._token)
            async for response in behavior(request_iterator, context):
                yield response

        return authenticated


def _require_loopback_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("worker server host must be a loopback IP address") from error

    if not address.is_loopback:
        raise ValueError("worker server host must be a loopback IP address")


def _grpc_target(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _publish_readiness(ready_file: Path, host: str, port: int) -> None:
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=ready_file.parent, prefix=f".{ready_file.name}.", suffix=".tmp"
    )
    temporary_file = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"host": host, "port": port}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_file, ready_file)
    finally:
        temporary_file.unlink(missing_ok=True)


async def serve_worker(
    servicer: engine_pb2_grpc.EngineWorkerServicer,
    host: str,
    port: int,
    token: str,
    ready_file: Path,
) -> None:
    """Run an authenticated engine worker on an ephemeral loopback port."""
    _require_loopback_host(host)
    server = grpc.aio.server(interceptors=[_WorkerTokenInterceptor(token)])
    engine_pb2_grpc.add_EngineWorkerServicer_to_server(servicer, server)
    bound_port = server.add_insecure_port(_grpc_target(host, port))
    if bound_port == 0:
        raise RuntimeError("worker server could not bind the requested address")

    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown_requested.set)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed_signals.append(signum)

    termination_task: asyncio.Task[bool] | None = None
    shutdown_task: asyncio.Task[bool] | None = None
    try:
        await server.start()
        _publish_readiness(ready_file, host, bound_port)

        termination_task = asyncio.create_task(server.wait_for_termination())
        shutdown_task = asyncio.create_task(shutdown_requested.wait())
        await asyncio.wait(
            (termination_task, shutdown_task), return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        try:
            for task in (termination_task, shutdown_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (termination_task, shutdown_task) if task is not None),
                return_exceptions=True,
            )
            for signum in installed_signals:
                loop.remove_signal_handler(signum)
            await server.stop(0)
        finally:
            ready_file.unlink(missing_ok=True)
