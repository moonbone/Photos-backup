"""Ingest: dedup, metadata, date provenance, derivatives, instances."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from photovault.hashing import sha256_file
from photovault.ingest import ingest_path, object_path
from photovault.models import Asset, AssetInstance, IngestRun
from photovault.thumbs import thumb_path

from .conftest import make_jpeg


def test_mixed_folder(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "with_exif.jpg", (10, 20, 30), exif_dt="2023:05:01 09:15:00",
              exif_offset="+02:00")
    make_jpeg(staging / "IMG_20240711_183000.jpg", (40, 50, 60))
    make_jpeg(staging / "no_date_at_all.jpg", (70, 80, 90))
    (staging / "notes.txt").write_text("not media")  # ignored

    r = ingest_path(session, config, staging, "phone-a")
    assert r.files_seen == 3
    assert r.files_new == 3
    assert r.files_failed == 0

    by_name = {}
    for inst in session.scalars(select(AssetInstance)):
        by_name[inst.original_filename] = session.get(Asset, inst.sha256)

    a = by_name["with_exif.jpg"]
    assert a.capture_ts_source == "exif"
    assert a.capture_ts == "2023-05-01T09:15:00"
    assert a.capture_tz == "+02:00"
    assert a.kind == "photo"
    assert a.width == 320 and a.height == 240

    b = by_name["IMG_20240711_183000.jpg"]
    assert b.capture_ts_source == "filename"
    assert b.capture_ts == "2024-07-11T18:30:00"

    c = by_name["no_date_at_all.jpg"]
    assert c.capture_ts_source == "mtime"

    # originals live in the content-addressed library; thumbnails generated
    for asset in (a, b, c):
        assert object_path(config, asset.sha256, asset.ext).exists()
        assert asset.thumb_state == "done"
        assert thumb_path(config, asset.sha256).exists()
        assert asset.backup_state == "new"


def test_reingest_is_idempotent(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")

    r1 = ingest_path(session, config, staging, "phone-a")
    r2 = ingest_path(session, config, staging, "phone-a")
    assert r1.files_new == 1
    assert r2.files_new == 0
    assert r2.files_duplicate == 1
    assert session.scalar(select(AssetInstance.id).limit(1)) is not None
    assert len(list(session.scalars(select(Asset)))) == 1
    assert len(list(session.scalars(select(AssetInstance)))) == 1  # same path, no dup row


def test_same_photo_two_sources(config, session, tmp_path):
    p1 = make_jpeg(tmp_path / "staging" / "phone-a" / "IMG_20240711_183000.jpg", (1, 2, 3))
    p2 = tmp_path / "staging" / "dslr" / "copy.jpg"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_bytes(p1.read_bytes())

    ingest_path(session, config, p1.parent, "phone-a")
    r2 = ingest_path(session, config, p2.parent, "dslr")
    assert r2.files_duplicate == 1
    assets = list(session.scalars(select(Asset)))
    instances = list(session.scalars(select(AssetInstance)))
    assert len(assets) == 1  # one asset...
    assert len(instances) == 2  # ...seen on two sources


def test_raw_jpeg_pairing(config, session, tmp_path):
    staging = tmp_path / "staging" / "dslr"
    make_jpeg(staging / "DSC_0042.JPG", (5, 5, 5))
    # a fake RAW (not a real NEF, but pairing + cataloging shouldn't care)
    (staging / "DSC_0042.NEF").write_bytes(b"NEFDATA" * 1000)

    ingest_path(session, config, staging, "dslr")
    assets = list(session.scalars(select(Asset)))
    assert len(assets) == 2
    groups = {a.pair_group for a in assets}
    assert len(groups) == 1  # same stem+dir -> same pair group
    kinds = {a.kind for a in assets}
    assert kinds == {"photo", "raw"}


def test_sidecar_travels_with_asset(config, session, tmp_path):
    staging = tmp_path / "staging" / "dslr"
    make_jpeg(staging / "DSC_0100.JPG")
    (staging / "DSC_0100.xmp").write_text("<xmp/>")

    ingest_path(session, config, staging, "dslr")
    asset = session.scalar(select(Asset))
    assert asset.has_sidecar
    from photovault.ingest import sidecar_path

    assert sidecar_path(config, asset.sha256).exists()


def test_bad_file_does_not_kill_run(config, session, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "good_IMG_20240711_183000.jpg")
    bad = staging / "bad.jpg"
    bad.write_bytes(b"not a jpeg at all")

    r = ingest_path(session, config, staging, "phone-a")
    # the corrupt jpeg still hashes and catalogs fine (thumb fails, that's ok)
    assert r.files_new == 2
    bad_sha = sha256_file(bad)
    assert session.get(Asset, bad_sha).thumb_state == "failed"

    run = session.scalar(select(IngestRun).order_by(IngestRun.id.desc()))
    assert run.finished_at is not None
    assert run.files_new == 2
