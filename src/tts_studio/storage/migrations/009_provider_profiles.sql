CREATE TABLE provider_profiles (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind = 'openai_compatible'),
    label TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_env TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE generation_jobs ADD COLUMN provider_id TEXT;
CREATE INDEX idx_generation_jobs_provider ON generation_jobs(provider_id, state);
