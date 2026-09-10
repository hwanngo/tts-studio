ALTER TABLE model_events ADD COLUMN stream_kind TEXT;
ALTER TABLE model_events ADD COLUMN stream_id TEXT;

UPDATE model_events
SET stream_kind = 'download', stream_id = download_id
WHERE download_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_model_events_stream_id
    ON model_events(stream_kind, stream_id, id);

CREATE TABLE generation_jobs (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    text TEXT NOT NULL,
    retain_artifact INTEGER NOT NULL CHECK (retain_artifact IN (0, 1)),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'loading', 'generating', 'finalizing', 'completed', 'cancelled', 'failed')
    ),
    bytes_written INTEGER NOT NULL DEFAULT 0 CHECK (bytes_written >= 0),
    frame_count INTEGER NOT NULL DEFAULT 0 CHECK (frame_count >= 0),
    sample_rate INTEGER CHECK (sample_rate IS NULL OR sample_rate > 0),
    channel_count INTEGER CHECK (channel_count IS NULL OR channel_count > 0),
    artifact_id TEXT,
    correlation_id TEXT NOT NULL,
    cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0, 1)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_generation_jobs_state ON generation_jobs(state);
CREATE INDEX idx_generation_jobs_model ON generation_jobs(model_id, state);

CREATE TABLE audio_artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES generation_jobs(id) ON DELETE CASCADE,
    path TEXT NOT NULL CHECK (
        substr(path, 1, 6) = 'audio/'
        AND instr(path, char(92)) = 0
        AND instr(path, '//') = 0
        AND substr(path, -1, 1) <> '/'
        AND path NOT LIKE 'audio/.'
        AND path NOT LIKE 'audio/..'
        AND path NOT LIKE 'audio/./%'
        AND path NOT LIKE 'audio/../%'
        AND path NOT LIKE '%/./%'
        AND path NOT LIKE '%/../%'
        AND path NOT LIKE '%/.'
        AND path NOT LIKE '%/..'
    ),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256 TEXT NOT NULL,
    sample_rate INTEGER NOT NULL CHECK (sample_rate > 0),
    channel_count INTEGER NOT NULL CHECK (channel_count > 0),
    frame_count INTEGER NOT NULL CHECK (frame_count >= 0),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TEXT NOT NULL,
    retained_at TEXT NOT NULL
);

CREATE INDEX idx_audio_artifacts_history ON audio_artifacts(retained_at DESC, id DESC);
