from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from tts_studio.providers.domain import ProviderProfile
from tts_studio.providers.egress import ProviderEgressError, validate_provider_egress


class InvalidProviderProfileError(ValueError):
    pass


class ProviderSecretMissingError(RuntimeError):
    pass


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_provider_input(
    *, kind: str, label: str, base_url: str, model: str, api_key_env: str
) -> None:
    if kind != "openai_compatible":
        raise InvalidProviderProfileError("kind must be 'openai_compatible'")
    if not label.strip() or len(label.strip()) > 120:
        raise InvalidProviderProfileError("label must be between 1 and 120 characters")
    if not model.strip() or len(model.strip()) > 256:
        raise InvalidProviderProfileError("model must be between 1 and 256 characters")
    if not _ENV_NAME.fullmatch(api_key_env):
        raise InvalidProviderProfileError("api_key_env must be a valid environment variable name")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InvalidProviderProfileError("base_url must be an HTTPS or loopback HTTP URL")
    try:
        validate_provider_egress(base_url, resolve_dns=False)
    except ProviderEgressError as error:
        raise InvalidProviderProfileError(str(error)) from error


class ProviderService:
    def __init__(self, registry: object) -> None:
        self._registry = registry

    @staticmethod
    def resolve_api_key(profile: ProviderProfile) -> str:
        value = os.environ.get(profile.api_key_env)
        if not value:
            raise ProviderSecretMissingError(
                f"environment variable {profile.api_key_env} is not set"
            )
        return value
