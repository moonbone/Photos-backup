"""Thumbnail/preview generation (Pillow), with RAW embedded-preview extraction."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from .config import Config

THUMB_SIZE = 400
PREVIEW_SIZE = 2048

_EXIFTOOL = shutil.which("exiftool")
_FFMPEG = shutil.which("ffmpeg")

Image.MAX_IMAGE_PIXELS = 512_000_000  # allow large panoramas, still bounded


def _sharded(base: Path, sha256: str) -> Path:
    d = base / sha256[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sha256}.jpg"


def thumb_path(config: Config, sha256: str) -> Path:
    return config.thumbs_dir / sha256[:2] / f"{sha256}.jpg"


def preview_path(config: Config, sha256: str) -> Path:
    return config.previews_dir / sha256[:2] / f"{sha256}.jpg"


def _save_scaled(img: Image.Image, size: int, dest: Path) -> None:
    img = ImageOps.exif_transpose(img)
    img.thumbnail((size, size))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(dest, "JPEG", quality=85)


def _raw_embedded_jpeg(src: Path) -> bytes | None:
    if not _EXIFTOOL:
        return None
    for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
        proc = subprocess.run(
            [_EXIFTOOL, "-b", tag, str(src)], capture_output=True
        )
        if proc.returncode == 0 and len(proc.stdout) > 1000:
            return proc.stdout
    return None


def _video_frame(src: Path) -> bytes | None:
    if not _FFMPEG:
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        proc = subprocess.run(
            [_FFMPEG, "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(src),
             "-frames:v", "1", tmp.name],
            capture_output=True,
        )
        if proc.returncode == 0:
            data = Path(tmp.name).read_bytes()
            return data or None
    return None


def make_derivatives(config: Config, src: Path, sha256: str, kind: str) -> str:
    """Create thumb + preview for an asset. Returns thumb_state: done|failed."""
    try:
        if kind == "photo":
            with Image.open(src) as img:
                _save_scaled(img, PREVIEW_SIZE, _sharded(config.previews_dir, sha256))
            with Image.open(src) as img:
                _save_scaled(img, THUMB_SIZE, _sharded(config.thumbs_dir, sha256))
            return "done"
        if kind == "raw":
            data = _raw_embedded_jpeg(src)
        elif kind == "video":
            data = _video_frame(src)
        else:
            data = None
        if not data:
            return "failed"
        import io

        with Image.open(io.BytesIO(data)) as img:
            _save_scaled(img, PREVIEW_SIZE, _sharded(config.previews_dir, sha256))
        with Image.open(io.BytesIO(data)) as img:
            _save_scaled(img, THUMB_SIZE, _sharded(config.thumbs_dir, sha256))
        return "done"
    except Exception:
        return "failed"
