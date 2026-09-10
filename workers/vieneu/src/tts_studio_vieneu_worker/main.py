"""Command-line entry point for the isolated VieNeu engine worker."""

import argparse
import asyncio
from pathlib import Path

from tts_studio_worker_sdk.auth import consume_worker_token
from tts_studio_worker_sdk.server import serve_worker

from tts_studio_vieneu_worker.service import VieNeuEngineWorker


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the TTS Studio VieNeu engine worker")
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
    asyncio.run(
        serve_worker(
            VieNeuEngineWorker(token, args.data_dir),
            host=args.host,
            port=args.port,
            token=token,
            ready_file=args.ready_file,
        )
    )
