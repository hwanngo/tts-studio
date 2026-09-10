"""Small, injectable Hugging Face repository boundary for the VieNeu worker."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class RepositoryClient(Protocol):
    def model_info(self, repo_id: str, revision: str | None = None) -> object: ...

    def list_files(self, repo_id: str, revision: str) -> Sequence[object]: ...

    def snapshot_download(
        self, repo_id: str, revision: str, destination: Path, allow_patterns: Sequence[str]
    ) -> None: ...


class HuggingFaceRepositoryClient:
    """Adapt ``huggingface_hub`` to the narrow interface used by the worker."""

    def __init__(self) -> None:
        from huggingface_hub import HfApi

        self._api = HfApi()

    def model_info(self, repo_id: str, revision: str | None = None) -> object:
        return self._api.model_info(repo_id, revision=revision)

    def list_files(self, repo_id: str, revision: str) -> Sequence[object]:
        return self._api.list_repo_tree(repo_id, revision=revision, recursive=True, expand=False)

    def snapshot_download(
        self, repo_id: str, revision: str, destination: Path, allow_patterns: Sequence[str]
    ) -> None:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=repo_id,
            revision=revision,
            local_dir=str(destination),
            allow_patterns=list(allow_patterns),
            repo_type="model",
        )
