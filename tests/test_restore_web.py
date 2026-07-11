"""Restore round-trip, catalog rebuild, and the web UI/API."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from photovault.hashing import sha256_file
from photovault.ingest import ingest_path
from photovault.models import Asset
from photovault.restore import rebuild_from_bucket, restore
from photovault.sync import sync
from photovault.verify import verify_remote
from photovault.web.app import create_app

from .conftest import make_jpeg


def test_restore_roundtrip(config, session, storage, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    src = make_jpeg(staging / "IMG_20240711_183000.jpg", (3, 141, 59))
    original_sha = sha256_file(src)
    ingest_path(session, config, staging, "phone-a")
    sync(session, config, storage)

    dest = tmp_path / "restored"
    r = restore(session, config, dest, storage=storage)
    assert not r.failed
    assert len(r.restored) == 1
    out = r.restored[0]
    assert out.endswith("2024/07/IMG_20240711_183000.jpg")  # YYYY/MM/original-name
    assert sha256_file(out) == original_sha  # bytes identical


def test_restore_filters_by_date(config, session, storage, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg", (1, 0, 0))
    make_jpeg(staging / "IMG_20230101_120000.jpg", (0, 1, 0))
    ingest_path(session, config, staging, "phone-a")
    sync(session, config, storage)

    dest = tmp_path / "restored"
    r = restore(session, config, dest, date_from="2024-01-01", date_to="2024-12-31", storage=storage)
    assert len(r.restored) == 1
    assert "2024/07" in r.restored[0]


def test_rebuild_catalog_from_bucket(config, session, storage, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")
    ingest_path(session, config, staging, "phone-a")
    sync(session, config, storage)  # snapshots the catalog
    session.close()

    config.db_path.unlink()  # the host "dies"
    key = rebuild_from_bucket(config, storage=storage)
    assert key.startswith("catalog/snapshot-")

    from photovault.db import open_session

    with open_session(config) as s2:
        assets = list(s2.scalars(select(Asset)))
        assert len(assets) == 1
        assert assets[0].backup_state == "uploaded"


def _client(config, token=None):
    config.web.token = token
    return TestClient(create_app(config))


def test_web_timeline_and_detail(config, session, storage, tmp_path):
    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg", exif_dt="2024:07:11 18:30:00")
    ingest_path(session, config, staging, "phone-a")
    sync(session, config, storage)
    verify_remote(session, config, storage)
    sha = session.scalar(select(Asset.sha256))

    client = _client(config)
    r = client.get("/")
    assert r.status_code == 200
    assert "2024-07-11" in r.text
    assert sha in r.text

    r = client.get(f"/photo/{sha}")
    assert r.status_code == 200
    assert "date from exif" in r.text
    assert "verified" in r.text

    assert client.get(f"/thumb/{sha}").status_code == 200
    assert client.get(f"/original/{sha}").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.get("/photo/" + "0" * 64).status_code == 404


def test_web_filters(config, session, storage, tmp_path):
    staging_a = tmp_path / "staging" / "phone-a"
    staging_b = tmp_path / "staging" / "dslr"
    make_jpeg(staging_a / "IMG_20240711_183000.jpg", (1, 0, 0))
    make_jpeg(staging_b / "no_date.jpg", (0, 1, 0))
    ingest_path(session, config, staging_a, "phone-a")
    ingest_path(session, config, staging_b, "dslr")

    sha_filename = session.scalar(
        select(Asset.sha256).where(Asset.capture_ts_source == "filename")
    )
    sha_mtime = session.scalar(
        select(Asset.sha256).where(Asset.capture_ts_source == "mtime")
    )

    client = _client(config)
    r = client.get("/?ts_source=filename")
    assert sha_filename in r.text
    assert sha_mtime not in r.text

    r2 = client.get("/?source=dslr")
    assert sha_mtime in r2.text
    assert sha_filename not in r2.text


def test_web_token_auth(config, session, tmp_path):
    client = _client(config, token="s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/", params={"token": "wrong"}).status_code == 401
    assert client.get("/", params={"token": "s3cret"}).status_code == 200
    assert client.get("/", headers={"x-photovault-token": "s3cret"}).status_code == 200


def test_upload_endpoint(config, session, storage, tmp_path):
    client = _client(config, token="s3cret")
    photo = make_jpeg(tmp_path / "outside" / "IMG_20240711_183000.jpg", (7, 7, 7))
    sha = sha256_file(photo)

    r = client.post(
        "/api/upload",
        headers={"x-photovault-token": "s3cret"},
        files={"file": ("IMG_20240711_183000.jpg", photo.read_bytes(), "image/jpeg")},
        data={"sha256": sha},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == sha
    assert body["new"] is True

    # re-upload -> duplicate, not a second asset
    r2 = client.post(
        "/api/upload",
        headers={"x-photovault-token": "s3cret"},
        files={"file": ("IMG_20240711_183000.jpg", photo.read_bytes(), "image/jpeg")},
    )
    assert r2.json()["duplicate"] is True

    # corrupted-in-transit: declared sha doesn't match received bytes
    r3 = client.post(
        "/api/upload",
        headers={"x-photovault-token": "s3cret"},
        files={"file": ("x.jpg", b"garbled", "image/jpeg")},
        data={"sha256": sha},
    )
    assert r3.status_code == 400


def test_watch_ingests_stable_files(config, session, tmp_path):
    from photovault.watch import watch

    staging = tmp_path / "staging" / "phone-a"
    make_jpeg(staging / "IMG_20240711_183000.jpg")
    # two cycles: first sights the file, second sees it stable and ingests
    watch(config, interval=0.01, auto_sync=False, max_cycles=3, log=lambda *_: None)

    from photovault.db import open_session

    with open_session(config) as s:
        assert len(list(s.scalars(select(Asset)))) == 1
