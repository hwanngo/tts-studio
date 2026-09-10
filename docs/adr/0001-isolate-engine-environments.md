# Isolate every engine adapter environment

TTS model families often require conflicting Python and native dependencies. Each Engine Adapter therefore runs in a separate Worker process with its own locked `uv` environment; the FastAPI Core remains dependency-light and communicates only through the engine protocol. This costs process supervision and environment storage but prevents one model family from destabilizing every other interface.
