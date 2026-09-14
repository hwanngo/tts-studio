from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.server.app import create_app


def test_non_loopback_app_requires_a_configured_api_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="API token"):
        create_app(Settings(data_dir=tmp_path / "data", host="0.0.0.0"))


@pytest.mark.asyncio
async def test_configured_api_token_protects_public_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTS_STUDIO_TEST_API_TOKEN", "private-token-value")
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            host="0.0.0.0",
            api_token_env="TTS_STUDIO_TEST_API_TOKEN",
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/v1/system")
        invalid = await client.get(
            "/api/v1/system", headers={"Authorization": "Bearer wrong-token"}
        )
        valid = await client.get(
            "/api/v1/system", headers={"Authorization": "Bearer private-token-value"}
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "authentication_failed"
    assert missing.headers["www-authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert valid.status_code == 200
    assert "private-token-value" not in missing.text
    assert "private-token-value" not in invalid.text


@pytest.mark.asyncio
async def test_loopback_api_remains_open_without_token_configuration(tmp_path: Path) -> None:
    app = create_app(Settings.resolve(tmp_path / "data"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/system")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_loopback_core_rejects_an_unexpected_host_header(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", host="127.0.0.1", port=7860))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:7860",
    ) as client:
        response = await client.get(
            "/api/v1/system",
            headers={"Host": "attacker.example:7860"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "host_not_allowed"


@pytest.mark.asyncio
async def test_cross_origin_browser_mutation_is_rejected(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", host="127.0.0.1", port=7860))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:7860",
    ) as client:
        response = await client.post(
            "/api/v1/models/validate",
            headers={"Origin": "http://attacker.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://127.0.0.1:7860"},
        {},
    ],
    ids=["same-origin-browser", "native-client-without-origin"],
)
async def test_trusted_mutation_reaches_the_public_route(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", host="127.0.0.1", port=7860))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:7860",
    ) as client:
        response = await client.post("/api/v1/models/validate", headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_browser_can_authenticate_to_a_protected_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTS_STUDIO_TEST_API_TOKEN", "private-token-value")
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            host="192.0.2.10",
            port=7860,
            api_token_env="TTS_STUDIO_TEST_API_TOKEN",
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://192.0.2.10:7860",
    ) as client:
        response = await client.get(
            "/api/v1/system",
            headers={
                "Authorization": "Bearer private-token-value",
                "Origin": "http://192.0.2.10:7860",
            },
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_wildcard_listener_accepts_numeric_public_host_and_matching_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTS_STUDIO_TEST_API_TOKEN", "private-token-value")
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            host="0.0.0.0",
            port=7860,
            api_token_env="TTS_STUDIO_TEST_API_TOKEN",
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://192.0.2.10:7860",
        headers={"Authorization": "Bearer private-token-value"},
    ) as client:
        accepted = await client.post(
            "/api/v1/models/validate",
            headers={"Origin": "http://192.0.2.10:7860"},
        )
        rejected = await client.get(
            "/api/v1/system",
            headers={"Host": "attacker.example:7860"},
        )

    assert accepted.status_code == 422
    assert accepted.json()["error"]["code"] == "request_validation_failed"
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "host_not_allowed"
