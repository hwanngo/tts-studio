CREATE TABLE IF NOT EXISTS model_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    download_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_events_download_id_id ON model_events(download_id, id);

CREATE TABLE IF NOT EXISTS model_event_cursor (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_event_id INTEGER NOT NULL CHECK (last_event_id >= 0)
);

INSERT OR IGNORE INTO model_event_cursor(singleton, last_event_id) VALUES (1, 0);
