from dataclasses import dataclass


@dataclass(frozen=True)
class SavedVoice:
    id: str
    model_id: str
    label: str
    relative_path: str
    transcript: str | None
    created_at: str
    updated_at: str
