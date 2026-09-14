ALTER TABLE generation_jobs ADD COLUMN retry_of TEXT REFERENCES generation_jobs(id);
CREATE UNIQUE INDEX idx_generation_jobs_retry_of ON generation_jobs(retry_of)
    WHERE retry_of IS NOT NULL;

UPDATE generation_jobs SET text = ''
WHERE retain_artifact = 0 AND state IN ('completed', 'cancelled', 'failed');
