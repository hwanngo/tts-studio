# Keep managed data project-local in v1

V1 stores models, history, Worker environments, logs, and SQLite under a resolved `.tts-studio/` directory instead of platform application-data locations. This makes local state visible and portable during early development; `--data-dir` and `TTS_STUDIO_DATA_DIR` preserve explicit deployment control, and login-service installation records the absolute resolved path.
