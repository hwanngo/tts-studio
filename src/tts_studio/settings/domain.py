"""Immutable values for persisted Core settings."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class CoreSettings:
    """User-level settings persisted by the Core application."""

    retain_audio_by_default: bool
    artifact_max_age_days: int | None
    artifact_max_storage_bytes: int | None
    api_token_env: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.retain_audio_by_default, bool):
            raise TypeError("retain_audio_by_default must be a boolean")
        for value, field in (
            (self.artifact_max_age_days, "artifact_max_age_days"),
            (self.artifact_max_storage_bytes, "artifact_max_storage_bytes"),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError(f"{field} must be a positive integer or None")
        if self.api_token_env is not None and (
            not isinstance(self.api_token_env, str)
            or not self.api_token_env.strip()
            or "\0" in self.api_token_env
            or _ENV_NAME.fullmatch(self.api_token_env) is None
        ):
            raise ValueError("api_token_env must be a non-empty safe string or None")
