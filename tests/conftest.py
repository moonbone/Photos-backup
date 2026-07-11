from __future__ import annotations

import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from photovault.config import Config, SourceConfig, StorageConfig, WebConfig
from photovault.db import init_db
from photovault.storage import StorageClient

BUCKET = "test-photovault"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(
        library_path=tmp_path / "library",
        home_timezone="Asia/Jerusalem",
        storage=StorageConfig(bucket=BUCKET, region="us-east-1"),
        sources=[
            SourceConfig(name="phone-a", staging=str(tmp_path / "staging" / "phone-a")),
            SourceConfig(name="dslr", staging=str(tmp_path / "staging" / "dslr")),
        ],
        web=WebConfig(),
    )
    cfg.ensure_dirs()
    (tmp_path / "staging" / "phone-a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "staging" / "dslr").mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def session(config: Config):
    factory = init_db(config)
    s = factory()
    yield s
    s.close()


@pytest.fixture
def storage(config: Config):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield StorageClient(config)


def make_jpeg(
    path: Path,
    color=(120, 40, 40),
    size=(320, 240),
    exif_dt: str | None = None,
    exif_offset: str | None = None,
) -> Path:
    """Create a real JPEG; optionally stamp EXIF DateTimeOriginal via exiftool."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    if exif_dt:
        args = ["exiftool", "-overwrite_original", f"-DateTimeOriginal={exif_dt}"]
        if exif_offset:
            args.append(f"-OffsetTimeOriginal={exif_offset}")
        args.append(str(path))
        subprocess.run(args, check=True, capture_output=True)
    return path
