"""Command-line entry point for the fake engine worker."""

import argparse
import asyncio
from pathlib import Path

from tts_studio_worker_sdk.auth import consume_worker_token
from tts_studio_worker_sdk.server import serve_worker

from tts_studio_fake_worker.service import FakeEngineWorker


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the TTS Studio fake engine worker")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--owner-claim")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    token = consume_worker_token(args.token_file)
    staging_root = args.data_dir.expanduser().resolve(strict=True) / "staging"
    asyncio.run(
        serve_worker(
            FakeEngineWorker(token, staging_root),
            host=args.host,
            port=args.port,
            token=token,
            ready_file=args.ready_file,
        )
    )
