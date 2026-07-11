"""Streaming SHA-256 helpers. The hash is the canonical asset identity."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def hex_to_b64(hex_digest: str) -> str:
    """S3 ChecksumSHA256 wants the digest base64-encoded."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")
