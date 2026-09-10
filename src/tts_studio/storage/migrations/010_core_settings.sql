CREATE TABLE core_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    retain_audio_by_default INTEGER NOT NULL DEFAULT 1 CHECK (retain_audio_by_default IN (0, 1)),
    artifact_max_age_days INTEGER CHECK (artifact_max_age_days IS NULL OR artifact_max_age_days > 0),
    artifact_max_storage_bytes INTEGER CHECK (artifact_max_storage_bytes IS NULL OR artifact_max_storage_bytes > 0),
    api_token_env TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO core_settings(singleton, updated_at) VALUES (1, CURRENT_TIMESTAMP);
