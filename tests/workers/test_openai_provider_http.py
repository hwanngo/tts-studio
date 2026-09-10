import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "workers" / "openai_compatible" / "src"))

import tts_studio_openai_worker.http as http_module
from tts_studio_openai_worker.service import OpenAICompatibleWorker


class _Context:
    def invocation_metadata(self):
        return (("x-tts-worker-token", "secret"),)


async def _describe_capabilities():
    response = await OpenAICompatibleWorker("secret").Describe(None, _Context())
    return {cap.name for cap in response.capabilities if cap.supported}


def test_openai_adapter_advertises_only_supported_numeric_controls() -> None:
    capabilities = asyncio.run(_describe_capabilities())

    assert "speed" in capabilities
    assert "pitch" not in capabilities
    assert "volume" not in capabilities
    assert "inline_cues" not in capabilities


def test_provider_request_executor_is_bounded() -> None:
    assert http_module._PROVIDER_EXECUTOR._max_workers == 4


def test_request_includes_speed_when_provider_supports_it(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            del size
            if getattr(self, "_read", False):
                return b""
            self._read = True
            return b"RIFF"  # request body is the only assertion in this test

    class Opener:
        def open(self, request, timeout):
            captured["body"] = json.loads(request.data)
            return Response()

    monkeypatch.setattr(http_module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(http_module, "validate_provider_egress", lambda url: None)
    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\0\0"))

    http_module._request("https://provider.test", "secret", "model", "hello", "alloy", speed=1.25)

    assert captured["body"]["speed"] == 1.25


def test_request_uses_configured_timeout(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            del size
            if getattr(self, "_read", False):
                return b""
            self._read = True
            return b"RIFF"

    class Opener:
        def open(self, request, timeout):
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(http_module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(http_module, "validate_provider_egress", lambda url: None)
    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\0\0"))

    http_module._request("https://provider.test", "secret", "model", "hello", "alloy", timeout=3.5)

    assert captured["timeout"] == 3.5


def test_request_omits_speed_when_not_provided(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            del size
            if getattr(self, "_read", False):
                return b""
            self._read = True
            return b"RIFF"

    class Opener:
        def open(self, request, timeout):
            captured["body"] = json.loads(request.data)
            return Response()

    monkeypatch.setattr(http_module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(http_module, "validate_provider_egress", lambda url: None)
    monkeypatch.setattr(http_module, "validate_wav", lambda payload: (48000, 1, b"\0\0"))

    http_module._request("https://provider.test", "secret", "model", "hello", "alloy")

    assert "speed" not in captured["body"]
