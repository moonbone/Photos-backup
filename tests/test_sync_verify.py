"""Sync + verify against mocked S3: state machine, resumability, corruption."""
from __future__ import annotations

import json

from sqlalchemy import select

from photovault.ingest import ingest_path
from photovault.models import Asset, RemoteCopy
from photovault.sync import sync
from photovault.verify import verify_remote

from .conftest import BUCKET, make_jpeg


def _ingest_some(config, session, tmp_path, n=3):
    staging = tmp_path / "staging" / "phone-a"
    for i in range(n):
        make_jpeg(staging / f"IMG_2024071{i}_18300{i}.jpg", (i * 20, 100, 100))
    return ingest_path(session, config, staging, "phone-a")


def test_sync_uploads_and_snapshots(config, session, storage, tmp_path):
    _ingest_some(config, session, tmp_path)
    r = sync(session, config, storage)
    assert r.uploaded == 3
    assert r.failed == 0

    for a in session.scalars(select(Asset)):
        assert a.backup_state == "uploaded"
        copy = session.get(RemoteCopy, (a.sha256, BUCKET))
        assert copy is not None
        # object actually in the bucket with the right bytes
        data = storage.get_bytes(a.storage_key)
        import hashlib

        assert hashlib.sha256(data).hexdigest() == a.sha256

    # catalog snapshot + manifest present and self-describing
    manifest = json.loads(storage.get_bytes("catalog/manifest.json"))
    assert manifest["snapshot_key"] == r.snapshot_key
    assert len(manifest["objects"]) == 3
    assert storage.get_bytes(r.snapshot_key)  # snapshot db exists


def test_sync_is_resumable(config, session, storage, tmp_path):
    _ingest_some(config, session, tmp_path)
    sync(session, config, storage)
    # new file arrives; only it is uploaded on the next run
    make_jpeg(tmp_path / "staging" / "phone-a" / "IMG_20240720_120000.jpg", (9, 9, 9))
    ingest_path(session, config, tmp_path / "staging" / "phone-a", "phone-a")
    r2 = sync(session, config, storage)
    assert r2.uploaded == 1


def test_verify_promotes_to_verified(config, session, storage, tmp_path):
    _ingest_some(config, session, tmp_path)
    sync(session, config, storage)
    r = verify_remote(session, config, storage)
    assert r.ok
    assert r.promoted == 3
    for a in session.scalars(select(Asset)):
        assert a.backup_state == "verified"
    # deep verify ran on the sample
    assert r.deep_verified == 3
    copy = session.scalar(select(RemoteCopy))
    assert copy.last_verify_method == "deep"
    assert copy.last_verified_at is not None


def test_verify_detects_deleted_object_and_resync_heals(config, session, storage, tmp_path):
    _ingest_some(config, session, tmp_path)
    sync(session, config, storage)
    verify_remote(session, config, storage)

    victim = session.scalar(select(Asset))
    storage.delete(victim.storage_key)  # someone deletes behind our back

    r = verify_remote(session, config, storage)
    assert not r.ok
    assert victim.sha256 in r.missing
    session.refresh(victim)
    assert victim.backup_state == "new"  # demoted and re-queued

    r2 = sync(session, config, storage)  # re-sync heals
    assert r2.uploaded == 1
    r3 = verify_remote(session, config, storage)
    assert r3.ok


def test_deep_verify_detects_corruption(config, session, storage, tmp_path):
    _ingest_some(config, session, tmp_path, n=1)
    sync(session, config, storage)
    verify_remote(session, config, storage)

    a = session.scalar(select(Asset))
    # corrupt the object in place (bypassing checksum enforcement)
    storage.client.put_object(
        Bucket=BUCKET, Key=a.storage_key, Body=b"X" * a.size_bytes
    )
    r = verify_remote(session, config, storage)
    assert a.sha256 in r.mismatched
    session.refresh(a)
    assert a.backup_state == "new"


def test_verify_reports_orphans(config, session, storage, tmp_path):
    _ingest_some(config, session, tmp_path, n=1)
    sync(session, config, storage)
    storage.put_bytes(b"stray", "originals/zz/not-in-catalog.jpg")
    r = verify_remote(session, config, storage)
    assert "originals/zz/not-in-catalog.jpg" in r.orphaned
    assert r.ok  # orphans are reported, not errors


def test_corrupted_local_file_is_never_uploaded(config, session, storage, tmp_path):
    """Layer 1: bytes that don't match the cataloged hash are refused pre-upload.

    (Real S3 additionally validates ChecksumSHA256 server-side; moto does not,
    so this exercises the client-side half of the guarantee.)
    """
    import pytest

    from photovault.storage import ChecksumMismatch

    staging = tmp_path / "staging" / "phone-a"
    f = make_jpeg(staging / "IMG_20240711_183000.jpg")
    with pytest.raises(ChecksumMismatch):
        storage.put_file(f, "originals/te/test.jpg", "0" * 64)
    # nothing landed in the bucket
    assert list(storage.list_all("originals/te/")) == []


def test_sync_skips_locally_corrupted_file(config, session, storage, tmp_path):
    """A library object corrupted after ingest stays `new` and is reported."""
    from photovault.ingest import object_path

    _ingest_some(config, session, tmp_path, n=1)
    a = session.scalar(select(Asset))
    object_path(config, a.sha256, a.ext).write_bytes(b"bitrot")

    r = sync(session, config, storage)
    assert r.uploaded == 0
    assert r.failed == 1
    session.refresh(a)
    assert a.backup_state == "new"
