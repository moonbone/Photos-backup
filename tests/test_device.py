"""The trust-before-delete flow, end to end."""
from __future__ import annotations

import json

from photovault.device import (
    CLASS_KNOWN_NOT_UPLOADED,
    CLASS_SAFE,
    CLASS_UNKNOWN,
    CLASS_UPLOADED_UNVERIFIED,
    check_device,
    prune_device,
)
from photovault.ingest import ingest_path
from photovault.sync import sync
from photovault.verify import verify_remote

from .conftest import make_jpeg


def test_full_lifecycle_to_safe(config, session, storage, tmp_path):
    device = tmp_path / "device"
    make_jpeg(device / "DCIM" / "IMG_20240711_183000.jpg", (1, 1, 1))

    # never ingested -> UNKNOWN
    c = check_device(session, config, device)
    assert c.files[0]["class"] == CLASS_UNKNOWN
    assert not c.all_safe

    # ingested but not uploaded -> KNOWN-NOT-UPLOADED
    ingest_path(session, config, device, "phone-a")
    c = check_device(session, config, device)
    assert c.files[0]["class"] == CLASS_KNOWN_NOT_UPLOADED

    # uploaded but not independently verified -> UPLOADED-UNVERIFIED
    sync(session, config, storage)
    c = check_device(session, config, device)
    assert c.files[0]["class"] == CLASS_UPLOADED_UNVERIFIED

    # verified -> SAFE
    verify_remote(session, config, storage)
    c = check_device(session, config, device)
    assert c.files[0]["class"] == CLASS_SAFE
    assert c.all_safe


def test_one_unknown_file_blocks_all_safe(config, session, storage, tmp_path):
    device = tmp_path / "device"
    make_jpeg(device / "IMG_20240711_183000.jpg", (1, 1, 1))
    ingest_path(session, config, device, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)

    make_jpeg(device / "IMG_20240712_090000.jpg", (2, 2, 2))  # taken after backup
    c = check_device(session, config, device)
    assert not c.all_safe
    classes = {f["path"]: f["class"] for f in c.files}
    assert classes["IMG_20240711_183000.jpg"] == CLASS_SAFE
    assert classes["IMG_20240712_090000.jpg"] == CLASS_UNKNOWN


def test_prune_dry_run_deletes_nothing(config, session, storage, tmp_path):
    device = tmp_path / "device"
    f = make_jpeg(device / "IMG_20240711_183000.jpg")
    ingest_path(session, config, device, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)
    c = check_device(session, config, device)

    r = prune_device(config, device, c.report_path, execute=False)
    assert r.dry_run
    assert r.deleted == ["IMG_20240711_183000.jpg"]
    assert f.exists()  # dry-run: still there


def test_prune_execute_deletes_only_safe(config, session, storage, tmp_path):
    device = tmp_path / "device"
    safe = make_jpeg(device / "IMG_20240711_183000.jpg", (1, 1, 1))
    ingest_path(session, config, device, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)
    unknown = make_jpeg(device / "IMG_20240712_090000.jpg", (2, 2, 2))  # after backup

    c = check_device(session, config, device)
    r = prune_device(config, device, c.report_path, execute=True)
    assert r.deleted == ["IMG_20240711_183000.jpg"]
    assert not safe.exists()
    assert unknown.exists()  # never SAFE, never touched
    assert r.log_path is not None
    log = [json.loads(line) for line in open(r.log_path)]
    assert log[0]["path"] == "IMG_20240711_183000.jpg"


def test_prune_skips_file_modified_after_report(config, session, storage, tmp_path):
    device = tmp_path / "device"
    f = make_jpeg(device / "IMG_20240711_183000.jpg", (1, 1, 1))
    ingest_path(session, config, device, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)
    c = check_device(session, config, device)

    f.write_bytes(b"EDITED AFTER THE REPORT")  # bytes changed -> not the SAFE bytes
    r = prune_device(config, device, c.report_path, execute=True)
    assert r.deleted == []
    assert r.skipped_changed == ["IMG_20240711_183000.jpg"]
    assert f.exists()


def test_prune_refuses_stale_report(config, session, storage, tmp_path):
    import pytest

    device = tmp_path / "device"
    make_jpeg(device / "IMG_20240711_183000.jpg")
    ingest_path(session, config, device, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)
    c = check_device(session, config, device)

    # backdate the report
    report = json.loads(open(c.report_path).read())
    report["generated_at"] = "2020-01-01T00:00:00Z"
    with open(c.report_path, "w") as fh:
        json.dump(report, fh)

    with pytest.raises(ValueError, match="older than"):
        prune_device(config, device, c.report_path, execute=True)


def test_prune_refuses_wrong_device(config, session, storage, tmp_path):
    import pytest

    device = tmp_path / "device"
    other = tmp_path / "other-device"
    other.mkdir()
    make_jpeg(device / "IMG_20240711_183000.jpg")
    ingest_path(session, config, device, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)
    c = check_device(session, config, device)

    with pytest.raises(ValueError, match="generated for"):
        prune_device(config, other, c.report_path, execute=True)
