from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from tts_studio_openai_worker import http as http_module


class _RedirectHandler(BaseHTTPRequestHandler):
    destination: str | None = None
    received_authorizations: ClassVar[list[str | None]] = []

    def _respond(self) -> None:
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
        type(self).received_authorizations.append(self.headers.get("Authorization"))
        if self.path == "/audio/speech" and type(self).destination is not None:
            self.send_response(302)
            self.send_header("Location", type(self).destination)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"RIFF")

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def log_message(self, format: str, *args: object) -> None:
        return


class _BlockedResponseHandler(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", "4")
        self.end_headers()
        type(self).started.set()
        try:
            type(self).release.wait()
            self.wfile.write(b"RIFF")
        except OSError:
            pass
        finally:
            type(self).finished.set()

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server:
    def __init__(self, handler: type[_RedirectHandler]) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def test_cross_origin_redirect_never_forwards_provider_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination_handler = type("DestinationHandler", (_RedirectHandler,), {})
    destination_handler.received_authorizations = []
    destination = _Server(destination_handler)

    origin_handler = type("OriginHandler", (_RedirectHandler,), {})
    origin_handler.received_authorizations = []
    origin_handler.destination = f"{destination.url}/redirected"
    origin = _Server(origin_handler)

    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\0\0"))
    try:
        with pytest.raises(http_module.ProviderRequestError, match="redirect") as error:
            http_module._request(origin.url, "secret", "model", "hello", "alloy")
    finally:
        origin.close()
        destination.close()

    assert error.value.code == "provider_redirect_rejected"
    assert origin_handler.received_authorizations == ["Bearer secret"]
    assert destination_handler.received_authorizations == []


def test_same_origin_redirect_preserves_provider_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = type("SameOriginHandler", (_RedirectHandler,), {})
    handler.received_authorizations = []
    server = _Server(handler)
    handler.destination = f"{server.url}/redirected"

    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\0\0"))
    try:
        http_module._request(server.url, "secret", "model", "hello", "alloy")
    finally:
        server.close()

    assert handler.received_authorizations == ["Bearer secret", "Bearer secret"]


def test_provider_request_rejects_dns_failure_without_sending_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_resolution(*args, **kwargs):
        raise OSError("DNS unavailable")

    monkeypatch.setattr(http_module.socket, "getaddrinfo", fail_resolution)
    with pytest.raises(http_module.ProviderRequestError) as error:
        http_module._request("https://provider.example/v1", "secret", "model", "hello", "alloy")

    assert error.value.code == "provider_egress_rejected"


def test_provider_request_rejects_private_rebinding_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def rebinding_resolution(host, port, *args, **kwargs):
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "10.0.0.1"
        return [
            (http_module.socket.AF_INET, http_module.socket.SOCK_STREAM, 6, "", (address, port))
        ]

    monkeypatch.setattr(http_module.socket, "getaddrinfo", rebinding_resolution)
    with pytest.raises(http_module.ProviderRequestError) as error:
        http_module._request("https://provider.example/v1", "secret", "model", "hello", "alloy")

    assert error.value.code == "provider_egress_rejected"
    assert calls >= 2


def test_provider_request_rejects_private_https_destination() -> None:
    with pytest.raises(http_module.ProviderRequestError) as error:
        http_module._request("https://127.0.0.1:443/v1", "secret", "model", "hello", "alloy")

    assert error.value.code == "provider_egress_rejected"


@pytest.mark.asyncio
async def test_blocked_provider_response_is_cancelled_at_transport_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = type("BlockedHandler", (_BlockedResponseHandler,), {})
    handler.started = threading.Event()
    handler.release = threading.Event()
    handler.finished = threading.Event()
    server = _Server(handler)
    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\\0\\0"))
    cancellation = http_module.ProviderCancellation()
    task = asyncio.create_task(
        http_module.synthesize(
            base_url=server.url,
            api_key="secret",
            model="model",
            text="hello",
            voice="alloy",
            cancellation=cancellation,
            timeout=0.5,
        )
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(handler.started.wait, 1.0), timeout=1.0)
        cancellation.cancel()
        with pytest.raises(http_module.ProviderRequestError) as error:
            await asyncio.wait_for(task, timeout=1.0)
        assert error.value.code == "synthesis_cancelled"
        handler.release.set()
        assert handler.finished.wait(timeout=1.0)
    finally:
        handler.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()


@pytest.mark.asyncio
async def test_cancelled_provider_request_quiesces_executor_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    finished = threading.Event()

    def blocking_request(*args, **kwargs):
        cancellation = kwargs.get("cancellation") or args[-1]
        started.set()
        while not cancellation.cancelled():
            threading.Event().wait(0.01)
        finished.set()
        raise http_module._cancelled_provider_error()

    monkeypatch.setattr(http_module, "_request", blocking_request)
    task = asyncio.create_task(
        http_module.synthesize(
            base_url="https://provider.example/v1",
            api_key="secret",
            model="model",
            text="hello",
            voice="alloy",
        )
    )
    await asyncio.to_thread(started.wait, 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_non_redirect_provider_request_keeps_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = type("OrdinaryHandler", (_RedirectHandler,), {})
    handler.received_authorizations = []
    server = _Server(handler)

    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\0\0"))
    try:
        result = http_module._request(server.url, "secret", "model", "hello", "alloy")
    finally:
        server.close()

    assert result.sample_rate_hz == 48000
    assert handler.received_authorizations == ["Bearer secret"]
