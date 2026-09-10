from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    kind: str
    label: str
    base_url: str
    model: str
    api_key_env: str
    created_at: str
    updated_at: str
