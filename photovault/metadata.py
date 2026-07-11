"""Metadata extraction: ExifTool when available (the norm), Pillow fallback."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

RAW_EXTS = {"nef", "cr2", "cr3", "arw", "dng", "orf", "rw2", "raf", "pef", "srw"}
PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "webp", "tif", "tiff", "bmp", "gif"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mts", "m2ts", "3gp", "mkv", "webm"}
MEDIA_EXTS = RAW_EXTS | PHOTO_EXTS | VIDEO_EXTS
SIDECAR_EXTS = {"xmp"}

_EXIFTOOL = shutil.which("exiftool")

# ExifTool tag names we lift into catalog columns; the full dump is kept as JSON.
_CORE_TAGS = [
    "DateTimeOriginal",
    "CreateDate",
    "CreationDate",
    "OffsetTimeOriginal",
    "OffsetTime",
    "Make",
    "Model",
    "LensID",
    "LensModel",
    "GPSLatitude",
    "GPSLongitude",
    "ImageWidth",
    "ImageHeight",
    "MIMEType",
    "Orientation",
]


def kind_for_ext(ext: str) -> str:
    e = ext.lower()
    if e in RAW_EXTS:
        return "raw"
    if e in PHOTO_EXTS:
        return "photo"
    if e in VIDEO_EXTS:
        return "video"
    return "other"


def extract_batch(paths: list[Path]) -> dict[Path, dict]:
    """Extract metadata for a batch of files. Returns {path: exiftool-style dict}."""
    if not paths:
        return {}
    if _EXIFTOOL:
        return _extract_exiftool(paths)
    return {p: _extract_pillow(p) for p in paths}


def _extract_exiftool(paths: list[Path]) -> dict[Path, dict]:
    cmd = [_EXIFTOOL, "-json", "-n", "-fast2", "-api", "largefilesupport=1"]
    cmd += [str(p) for p in paths]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    results: dict[Path, dict] = {p: {} for p in paths}
    if not proc.stdout.strip():
        return results
    try:
        entries = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return results
    for entry in entries:
        src = entry.get("SourceFile")
        if src:
            results[Path(src)] = entry
    return results


def _extract_pillow(path: Path) -> dict:
    """Minimal fallback when exiftool is absent: JPEG/TIFF EXIF via Pillow."""
    try:
        from PIL import ExifTags, Image

        with Image.open(path) as img:
            out: dict = {
                "MIMEType": Image.MIME.get(img.format or "", None),
                "ImageWidth": img.width,
                "ImageHeight": img.height,
            }
            exif = img.getexif()
            ifd = exif.get_ifd(ExifTags.IFD.Exif) if exif else {}
            if v := ifd.get(ExifTags.Base.DateTimeOriginal):
                out["DateTimeOriginal"] = v
            if v := ifd.get(ExifTags.Base.OffsetTimeOriginal):
                out["OffsetTimeOriginal"] = v
            if v := exif.get(ExifTags.Base.DateTime):
                out.setdefault("CreateDate", v)
            if v := exif.get(ExifTags.Base.Make):
                out["Make"] = v
            if v := exif.get(ExifTags.Base.Model):
                out["Model"] = v
            return out
    except Exception:
        return {}


def core_fields(meta: dict) -> dict:
    """Lift the catalog-column fields out of a full metadata dict."""
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "camera_make": meta.get("Make"),
        "camera_model": meta.get("Model"),
        "lens": meta.get("LensID") or meta.get("LensModel"),
        "gps_lat": _num(meta.get("GPSLatitude")),
        "gps_lon": _num(meta.get("GPSLongitude")),
        "width": _int(meta.get("ImageWidth")),
        "height": _int(meta.get("ImageHeight")),
        "mime": meta.get("MIMEType"),
    }


def exif_json_dump(meta: dict) -> str:
    clean = {k: v for k, v in meta.items() if k != "SourceFile"}
    return json.dumps(clean, default=str, ensure_ascii=False)
