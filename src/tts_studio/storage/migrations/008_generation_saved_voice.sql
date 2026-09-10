ALTER TABLE generation_jobs ADD COLUMN saved_voice_id TEXT;
CREATE INDEX idx_generation_jobs_saved_voice ON generation_jobs(saved_voice_id);
