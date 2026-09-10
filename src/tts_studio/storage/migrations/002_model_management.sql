CREATE TABLE engine_installations (
    id TEXT PRIMARY KEY,
    engine_id TEXT NOT NULL,
    version TEXT NOT NULL,
    command_json TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    last_error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (engine_id, version)
);

CREATE TABLE model_installations (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL UNIQUE,
    requested_revision TEXT,
    resolved_commit TEXT NOT NULL,
    engine_installation_id TEXT NOT NULL,
    compatibility_evidence_json TEXT NOT NULL,
    runtime_variant TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    checksum_summary_json TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    cache_path TEXT NOT NULL,
    desired_load_state TEXT NOT NULL,
    observed_load_state TEXT NOT NULL,
    replica_summary_json TEXT NOT NULL,
    last_error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (engine_installation_id) REFERENCES engine_installations(id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_model_installations_engine
    ON model_installations(engine_installation_id);

CREATE TABLE download_jobs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    requested_revision TEXT,
    engine_installation_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'queued', 'validating', 'downloading', 'verifying',
            'activating', 'completed', 'cancelled', 'failed'
        )
    ),
    bytes_downloaded INTEGER NOT NULL DEFAULT 0 CHECK (bytes_downloaded >= 0),
    total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 0),
    phase TEXT NOT NULL,
    staging_path TEXT NOT NULL,
    target_model_id TEXT,
    cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0, 1)),
    correlation_id TEXT NOT NULL,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (engine_installation_id) REFERENCES engine_installations(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (target_model_id) REFERENCES model_installations(id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    CHECK (total_bytes IS NULL OR bytes_downloaded <= total_bytes)
);

CREATE INDEX idx_download_jobs_repository_id ON download_jobs(repository_id);
CREATE INDEX idx_download_jobs_state ON download_jobs(state);
CREATE INDEX idx_download_jobs_engine ON download_jobs(engine_installation_id);
