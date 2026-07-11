"""S3-compatible storage client (AWS S3, Backblaze B2, R2, Wasabi, MinIO)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import boto3
from botocore.config import Config as BotoConfig

from .config import Config
from .hashing import hex_to_b64, sha256_file


class ChecksumMismatch(Exception):
    """Local bytes no longer match the cataloged hash — refuse to upload."""


class StorageClient:
    def __init__(self, config: Config):
        if not config.storage.bucket:
            raise RuntimeError("No [storage] bucket configured")
        key, secret = config.storage.resolved_credentials()
        self.bucket = config.storage.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=config.storage.endpoint,
            region_name=config.storage.region,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            config=BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"}),
        )

    def put_file(self, path: Path, key: str, sha256_hex: str) -> str:
        """Upload with server-side SHA-256 verification.

        The file is re-hashed immediately before upload (catching local
        corruption since ingest), and the server recomputes the checksum of
        the received bytes and rejects the request on mismatch — integrity
        Layer 1 (docs/INTEGRITY.md). Single-request uploads support objects
        up to 5 GB, ample for photos. Returns the confirmed base64 checksum.
        """
        actual = sha256_file(path)
        if actual != sha256_hex:
            raise ChecksumMismatch(
                f"{path} hashes to {actual}, catalog says {sha256_hex} — refusing to upload"
            )
        checksum = hex_to_b64(sha256_hex)
        with open(path, "rb") as f:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=f,
                ChecksumSHA256=checksum,
            )
        return checksum

    def put_bytes(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(dest))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_all(self, prefix: str) -> Iterator[tuple[str, int]]:
        """Yield (key, size) for every object under prefix."""
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"], obj["Size"]
