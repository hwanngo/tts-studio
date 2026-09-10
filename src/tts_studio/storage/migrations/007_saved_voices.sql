CREATE TABLE saved_voices (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    label TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE CHECK (
        substr(relative_path, 1, 7) = 'voices/'
        AND instr(relative_path, char(92)) = 0
        AND instr(relative_path, '//') = 0
        AND relative_path NOT LIKE '%/../%'
        AND relative_path NOT LIKE '%/./%'
    ),
    transcript TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_saved_voices_model ON saved_voices(model_id, created_at, id);
