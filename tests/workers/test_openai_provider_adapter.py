import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "workers" / "openai_compatible" / "src"))

from tts_studio_openai_worker import http as provider_http


@pytest.mark.asyncio
async def test_provider_timeout_maps_to_safe_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_request(*args: object) -> object:
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(provider_http, "_request", slow_request)
    with pytest.raises(provider_http.ProviderRequestError) as raised:
        await provider_http.synthesize(
            base_url="https://provider.example/v1",
            api_key="secret",
            model="tts-1",
            text="hello",
            voice="alloy",
            timeout=0.001,
        )
    assert raised.value.code == "provider_timeout"
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)
