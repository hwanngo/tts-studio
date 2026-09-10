CREATE UNIQUE INDEX idx_audio_artifacts_job_id_id ON audio_artifacts(job_id, id);

CREATE TABLE generation_alignments (
    job_id TEXT PRIMARY KEY REFERENCES generation_jobs(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= 1048576)),
    error_json TEXT CHECK (error_json IS NULL OR (json_valid(error_json) AND length(CAST(error_json AS BLOB)) <= 16384)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id, artifact_id) REFERENCES audio_artifacts(job_id, id) ON DELETE CASCADE,
    CHECK (
        (state = 'completed' AND result_json IS NOT NULL AND error_json IS NULL)
        OR (state = 'failed' AND result_json IS NULL AND error_json IS NOT NULL)
        OR (state IN ('queued', 'running') AND result_json IS NULL AND error_json IS NULL)
    )
);

CREATE INDEX idx_generation_alignments_state ON generation_alignments(state);
