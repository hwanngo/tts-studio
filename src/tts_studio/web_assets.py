"""Packaged Web UI asset resolution and SPA routing."""

from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

_API_NAMESPACES = frozenset({"api", "v1"})
_MISSING_ASSETS_MESSAGE = (
    "Web assets are unavailable. Build the distribution to include the TTS Studio Web UI."
)


def _packaged_static_root() -> Path:
    return Path(str(resources.files("tts_studio").joinpath("static")))


def _is_api_path(path: str) -> bool:
    return path.partition("/")[0] in _API_NAMESPACES


def mount_web_app(app: FastAPI, static_root: Path | None = None) -> None:
    """Mount packaged frontend assets and a namespace-safe SPA fallback."""
    root = (static_root or _packaged_static_root()).resolve()
    index = root / "index.html"
    assets = root / "assets"

    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    def index_response() -> Response:
        if index.is_file():
            return FileResponse(index)
        return PlainTextResponse(_MISSING_ASSETS_MESSAGE, status_code=503)

    @app.get("/", include_in_schema=False)
    async def web_root() -> Response:
        return index_response()

    @app.get("/{path:path}", include_in_schema=False)
    async def web_fallback(path: str) -> Response:
        if _is_api_path(path):
            raise HTTPException(status_code=404)
        return index_response()
