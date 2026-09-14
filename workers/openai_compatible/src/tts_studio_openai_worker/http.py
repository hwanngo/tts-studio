from __future__ import annotations

import asyncio
import contextlib
import functools
import http.client
import json
import socket
import ssl
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import IO, Any
from urllib.parse import urlsplit

from tts_studio_worker_sdk.egress import (
    ProviderEgressError,
    resolve_provider_addresses,
    validate_provider_egress,
)
from tts_studio_worker_sdk.limits import MAX_SYNTHESIS_TEXT_CHARS

from tts_studio_openai_worker.wav import ProviderAudioError, validate_wav


@dataclass(frozen=True)
class ProviderSynthesis:
    sample_rate_hz: int
    frames: int
    pcm: bytes


_PROVIDER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="provider-http")
_PROVIDER_CANCEL_DRAIN_SECONDS = 0.25
_REQUEST_CONTEXT = threading.local()


class ProviderRequestError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class ProviderCancellation:
    """Cooperative cancellation for a blocking provider request."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._quarantined = threading.Event()
        self._lock = threading.Lock()
        self._response: object | None = None

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            response = self._response
        _close_response(response)

    def cancelled(self) -> bool:
        return self._event.is_set()

    def quarantine(self) -> None:
        self._quarantined.set()

    def quarantined(self) -> bool:
        return self._quarantined.is_set()

    def attach(self, response: object) -> None:
        with self._lock:
            self._response = response
        if self.cancelled():
            _close_response(response)

    def detach(self) -> None:
        with self._lock:
            self._response = None


def _cancelled_provider_error() -> ProviderRequestError:
    return ProviderRequestError(
        "synthesis_cancelled", "The provider request was cancelled.", retryable=False
    )


def _close_response(response: object | None) -> None:
    """Close urllib's response and its underlying socket, if exposed."""
    if response is None:
        return
    close = getattr(response, "close", None)
    if callable(close):
        close()
    current = response
    for attribute in ("fp", "raw", "_sock"):
        current = getattr(current, attribute, None)
        if current is None:
            return
        close = getattr(current, "close", None)
        if callable(close):
            close()


def _set_response_timeout(response: object, timeout: float) -> None:
    current = response
    for attribute in ("fp", "raw", "_sock"):
        current = getattr(current, attribute, None)
        if current is None:
            return
    settimeout = getattr(current, "settimeout", None)
    if callable(settimeout):
        settimeout(min(timeout, 0.25))


def _current_cancellation() -> ProviderCancellation | None:
    cancellation = getattr(_REQUEST_CONTEXT, "cancellation", None)
    return cancellation if isinstance(cancellation, ProviderCancellation) else None


def _raise_if_cancelled(cancellation: ProviderCancellation | None) -> None:
    if cancellation is not None and cancellation.cancelled():
        raise _cancelled_provider_error()


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = (
            443
            if parsed.scheme.lower() == "https"
            else 80
            if parsed.scheme.lower() == "http"
            else None
        )
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _connect_pinned(host: str, port: int, timeout: float | None, scheme: str) -> socket.socket:
    cancellation = _current_cancellation()
    _raise_if_cancelled(cancellation)
    target = f"{scheme}://{f'[{host}]' if ':' in host else host}:{port}"
    addresses = resolve_provider_addresses(target)
    _raise_if_cancelled(cancellation)
    last_error: OSError | None = None
    for address in addresses:
        _raise_if_cancelled(cancellation)
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(min(timeout, 0.25) if timeout is not None else 0.25)
            sock.connect((address, port, 0, 0) if family == socket.AF_INET6 else (address, port))
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    raise last_error or OSError("provider connection failed")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect_pinned(self.host, self.port, self.timeout, "http")

    def request(self, *args: Any, **kwargs: Any) -> None:
        _raise_if_cancelled(_current_cancellation())
        super().request(*args, **kwargs)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    # CPython initializes this SSLContext; typeshed omits the private attribute.
    _context: ssl.SSLContext

    def connect(self) -> None:
        self.sock = _connect_pinned(self.host, self.port, self.timeout, "https")
        _raise_if_cancelled(_current_cancellation())
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    def request(self, *args: Any, **kwargs: Any) -> None:
        _raise_if_cancelled(_current_cancellation())
        super().request(*args, **kwargs)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    # HTTPSHandler likewise stores the verified TLS context in CPython.
    _context: ssl.SSLContext

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            validate_provider_egress(newurl)
        except ProviderEgressError as error:
            raise ProviderRequestError(
                "provider_egress_rejected",
                "The provider destination is not permitted.",
                retryable=False,
            ) from error
        if _origin(req.full_url) != _origin(newurl):
            raise ProviderRequestError(
                "provider_redirect_rejected",
                "The provider redirected to a different origin.",
                retryable=False,
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            authorization = req.headers.get("Authorization")
            if authorization is not None:
                redirected.headers.pop("Authorization", None)
                redirected.add_header("Authorization", authorization)
        return redirected


async def synthesize(
    *,
    base_url: str,
    api_key: str,
    model: str,
    text: str,
    voice: str,
    speed: float | None = None,
    timeout: float = 60.0,
    cancellation: ProviderCancellation | None = None,
) -> ProviderSynthesis:
    cancellation = cancellation or ProviderCancellation()
    loop = asyncio.get_running_loop()
    request = functools.partial(
        _request, base_url, api_key, model, text, voice, speed, timeout, cancellation
    )
    future = loop.run_in_executor(_PROVIDER_EXECUTOR, request)
    deadline = loop.time() + timeout
    try:
        while True:
            if cancellation.cancelled():
                await _drain_or_quarantine(future, cancellation)
                raise _cancelled_provider_error()
            remaining = deadline - loop.time()
            if remaining <= 0:
                cancellation.cancel()
                await _drain_or_quarantine(future, cancellation)
                raise ProviderRequestError(
                    "provider_timeout", "The provider request timed out.", retryable=True
                )
            done, _ = await asyncio.wait((future,), timeout=min(0.01, remaining))
            if done:
                return future.result()
    except asyncio.CancelledError:
        cancellation.cancel()
        await _drain_or_quarantine(future, cancellation)
        raise


async def _drain_or_quarantine(
    future: asyncio.Future[ProviderSynthesis], cancellation: ProviderCancellation
) -> None:
    done, _ = await asyncio.wait((future,), timeout=_PROVIDER_CANCEL_DRAIN_SECONDS)
    if not done:
        cancellation.quarantine()
        future.add_done_callback(_consume_future_result)
        return
    try:
        future.result()
    except BaseException as cleanup_error:  # noqa: BLE001 - cancellation preserves caller outcome
        del cleanup_error


def _consume_future_result(future: asyncio.Future[ProviderSynthesis]) -> None:
    try:
        future.result()
    except BaseException as cleanup_error:  # noqa: BLE001 - consume quarantined operation outcome
        del cleanup_error


def _request(
    base_url: str,
    api_key: str,
    model: str,
    text: str,
    voice: str,
    speed: float | None = None,
    timeout: float = 60.0,
    cancellation: ProviderCancellation | None = None,
) -> ProviderSynthesis:
    _REQUEST_CONTEXT.cancellation = cancellation
    try:
        return _request_inner(base_url, api_key, model, text, voice, speed, timeout, cancellation)
    finally:
        with contextlib.suppress(AttributeError):
            del _REQUEST_CONTEXT.cancellation


def _request_inner(
    base_url: str,
    api_key: str,
    model: str,
    text: str,
    voice: str,
    speed: float | None = None,
    timeout: float = 60.0,
    cancellation: ProviderCancellation | None = None,
) -> ProviderSynthesis:
    if len(text) > MAX_SYNTHESIS_TEXT_CHARS:
        raise ProviderRequestError(
            "input_too_large", "The synthesis input is too large.", retryable=False
        )
    if cancellation is not None and cancellation.cancelled():
        raise _cancelled_provider_error()
    try:
        validate_provider_egress(base_url)
    except ProviderEgressError as error:
        raise ProviderRequestError(
            "provider_egress_rejected",
            "The provider destination is not permitted.",
            retryable=False,
        ) from error
    _raise_if_cancelled(cancellation)
    payload: dict[str, str | float] = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "wav",
    }
    if speed is not None:
        payload["speed"] = speed
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/audio/speech",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        _raise_if_cancelled(cancellation)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _PinnedHTTPHandler,
            _PinnedHTTPSHandler,
            _SafeRedirectHandler,
        )
        with opener.open(request, timeout=timeout) as response:
            if cancellation is not None:
                cancellation.attach(response)
            _set_response_timeout(response, timeout)
            try:
                chunks: list[bytes] = []
                total = 0
                while total <= 20 * 1024 * 1024:
                    if cancellation is not None and cancellation.cancelled():
                        raise _cancelled_provider_error()
                    try:
                        chunk = response.read(min(64 * 1024, 20 * 1024 * 1024 + 1 - total))
                    except TimeoutError:
                        if cancellation is not None and cancellation.cancelled():
                            raise _cancelled_provider_error()
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 20 * 1024 * 1024:
                        break
                audio_payload = b"".join(chunks)
            finally:
                if cancellation is not None:
                    cancellation.detach()
    except ProviderRequestError:
        raise
    except ProviderEgressError as error:
        raise ProviderRequestError(
            "provider_egress_rejected",
            "The provider destination is not permitted.",
            retryable=False,
        ) from error
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise ProviderRequestError(
                "provider_authentication_failed",
                "The provider rejected authentication.",
                retryable=False,
            ) from error
        if error.code == 429:
            raise ProviderRequestError(
                "provider_rate_limited", "The provider rate limit was reached.", retryable=True
            ) from error
        raise ProviderRequestError(
            "provider_unavailable", "The provider returned an unavailable response.", retryable=True
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if cancellation is not None and cancellation.cancelled():
            raise _cancelled_provider_error() from error
        raise ProviderRequestError(
            "provider_unavailable", "The provider could not be reached.", retryable=True
        ) from error
    try:
        rate, frames, pcm = validate_wav(audio_payload)
    except ProviderAudioError as error:
        raise ProviderRequestError(
            "provider_invalid_audio", "The provider returned unsupported audio.", retryable=False
        ) from error
    return ProviderSynthesis(rate, frames, pcm)
