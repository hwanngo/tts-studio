DROP INDEX IF EXISTS idx_generation_jobs_state;
DROP INDEX IF EXISTS idx_generation_jobs_model;
DROP INDEX IF EXISTS idx_audio_artifacts_history;

ALTER TABLE audio_artifacts RENAME TO audio_artifacts_legacy;
ALTER TABLE generation_jobs RENAME TO generation_jobs_legacy;
DROP INDEX IF EXISTS idx_generation_jobs_state;
DROP INDEX IF EXISTS idx_generation_jobs_model;
DROP INDEX IF EXISTS idx_audio_artifacts_history;

CREATE TABLE generation_jobs (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    voice_id TEXT,
    reference_id TEXT,
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
    updated_at TEXT NOT NULL,
    CHECK ((voice_id IS NULL AND reference_id IS NULL) OR ((voice_id IS NULL) <> (reference_id IS NULL)))
);

INSERT INTO generation_jobs (
    id, model_id, engine_id, voice_id, reference_id, text, retain_artifact, state,
    bytes_written, frame_count, sample_rate, channel_count, artifact_id,
    correlation_id, cancellation_requested, error_json, created_at, updated_at
)
SELECT id, model_id, engine_id, voice_id, NULL, text, retain_artifact, state,
       bytes_written, frame_count, sample_rate, channel_count, artifact_id,
       correlation_id, cancellation_requested, error_json, created_at, updated_at
FROM generation_jobs_legacy;

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

INSERT INTO audio_artifacts (
    id, job_id, path, byte_size, sha256, sample_rate, channel_count,
    frame_count, duration_ms, created_at, retained_at
)
SELECT id, job_id, path, byte_size, sha256, sample_rate, channel_count,
       frame_count, duration_ms, created_at, retained_at
FROM audio_artifacts_legacy;

CREATE INDEX idx_audio_artifacts_history ON audio_artifacts(retained_at DESC, id DESC);

DROP TABLE audio_artifacts_legacy;
DROP TABLE generation_jobs_legacy;
