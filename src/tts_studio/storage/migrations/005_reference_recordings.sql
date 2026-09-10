CREATE TABLE reference_recordings (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE CHECK (
        substr(relative_path, 1, 19) = 'staging/references/'
        AND instr(relative_path, char(92)) = 0
        AND instr(relative_path, '//') = 0
        AND relative_path NOT LIKE '%/../%'
        AND relative_path NOT LIKE '%/./%'
        AND relative_path NOT LIKE '%/%/%/%'
    ),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0 AND byte_size <= 20971520),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    container TEXT NOT NULL DEFAULT '',
    sample_rate_hz INTEGER NOT NULL DEFAULT 0 CHECK (sample_rate_hz >= 0),
    channels INTEGER NOT NULL DEFAULT 0 CHECK (channels >= 0),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    transcript_present INTEGER NOT NULL CHECK (transcript_present IN (0, 1)),
    state TEXT NOT NULL CHECK (
        state IN ('uploaded', 'validated', 'consumed', 'expired', 'deleted', 'cleanup_failed')
    ),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_reference_recordings_state_expires
    ON reference_recordings(state, expires_at, id);
