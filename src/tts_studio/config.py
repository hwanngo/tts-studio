from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Resolved configuration for a TTS Studio Core instance."""

    model_config = SettingsConfigDict(
        env_prefix="TTS_STUDIO_",
        frozen=True,
        validate_default=True,
    )

    data_dir: Path = Field(default_factory=lambda: Path.cwd() / ".tts-studio")
    host: str = "127.0.0.1"
    port: int = 7860
    api_token_env: str | None = None

    @field_validator("data_dir")
    @classmethod
    def resolve_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @classmethod
    def resolve(cls, data_dir: Path | None = None) -> Settings:
        """Resolve settings with an explicit data directory taking precedence."""
        if data_dir is not None:
            return cls(data_dir=data_dir)
        return cls()
