"""Shared Core generation admission bounds; Workers enforce their own boundary."""

MAX_SYNTHESIS_TEXT_CHARS = 10_000
# Fits 10,000 JSON-escaped Unicode characters plus ordinary request metadata.
MAX_GENERATION_BODY_BYTES = 128 * 1024
