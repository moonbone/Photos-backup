"""SQLAlchemy catalog models. See docs/ARCHITECTURE.md §4."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# backup_state values
STATE_NEW = "new"
STATE_UPLOADED = "uploaded"
STATE_VERIFIED = "verified"

# capture_ts_source values
TS_EXIF = "exif"
TS_FILENAME = "filename"
TS_MTIME = "mtime"
TS_MANUAL = "manual"


class Base(DeclarativeBase):
    pass


class Asset(Base):
    """One unique photo/video, identified by the SHA-256 of its bytes.

    capture_ts is stored as a naive local-clock ISO string
    ("YYYY-MM-DDTHH:MM:SS") so lexicographic order equals the order a human
    expects on a timeline; the UTC offset (when known) is kept in capture_tz.
    """

    __tablename__ = "assets"

    sha256: Mapped[str] = mapped_column(primary_key=True)
    size_bytes: Mapped[int]
    ext: Mapped[str]
    mime: Mapped[str | None]
    kind: Mapped[str]  # photo | raw | video | other
    capture_ts: Mapped[str | None]
    capture_tz: Mapped[str | None]  # e.g. "+03:00", None if unknown
    capture_ts_source: Mapped[str]  # exif | filename | mtime | manual
    date_only: Mapped[bool] = mapped_column(default=False)
    camera_make: Mapped[str | None]
    camera_model: Mapped[str | None]
    lens: Mapped[str | None]
    gps_lat: Mapped[float | None]
    gps_lon: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    exif_json: Mapped[str | None]
    backup_state: Mapped[str] = mapped_column(default=STATE_NEW)
    thumb_state: Mapped[str] = mapped_column(default="pending")  # pending | done | failed
    has_sidecar: Mapped[bool] = mapped_column(default=False)
    pair_group: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(default=utcnow_iso)

    __table_args__ = (
        Index("ix_assets_capture_ts", "capture_ts"),
        Index("ix_assets_backup_state", "backup_state"),
    )

    @property
    def storage_key(self) -> str:
        return f"originals/{self.sha256[:2]}/{self.sha256}.{self.ext}"

    @property
    def sidecar_key(self) -> str:
        return f"sidecars/{self.sha256[:2]}/{self.sha256}.xmp"


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[str] = mapped_column(default="folder")
    staging_path: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(default=utcnow_iso)


class AssetInstance(Base):
    """A sighting of an asset on a particular source (device/folder)."""

    __tablename__ = "asset_instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(ForeignKey("assets.sha256"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    original_path: Mapped[str]  # relative to the staging/device root
    original_filename: Mapped[str]
    file_mtime: Mapped[str | None]
    seen_at: Mapped[str] = mapped_column(default=utcnow_iso)
    ingest_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_runs.id"))

    __table_args__ = (
        UniqueConstraint("sha256", "source_id", "original_path", name="uq_instance"),
        Index("ix_instances_sha", "sha256"),
        Index("ix_instances_source", "source_id", "seen_at"),
    )


class RemoteCopy(Base):
    __tablename__ = "remote_copies"

    sha256: Mapped[str] = mapped_column(ForeignKey("assets.sha256"), primary_key=True)
    bucket: Mapped[str] = mapped_column(primary_key=True)
    storage_key: Mapped[str]
    size_bytes: Mapped[int]
    upload_checksum: Mapped[str]  # base64 SHA-256 confirmed by the server
    uploaded_at: Mapped[str] = mapped_column(default=utcnow_iso)
    last_verified_at: Mapped[str | None]
    last_verify_method: Mapped[str | None]  # listing | deep


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    started_at: Mapped[str] = mapped_column(default=utcnow_iso)
    finished_at: Mapped[str | None]
    files_seen: Mapped[int] = mapped_column(default=0)
    files_new: Mapped[int] = mapped_column(default=0)
    files_duplicate: Mapped[int] = mapped_column(default=0)
    files_failed: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str | None]


class VerifyRun(Base):
    __tablename__ = "verify_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[str] = mapped_column(default=utcnow_iso)
    finished_at: Mapped[str | None]
    kind: Mapped[str]  # listing | deep | device
    objects_checked: Mapped[int] = mapped_column(default=0)
    mismatches: Mapped[int] = mapped_column(default=0)
    missing: Mapped[int] = mapped_column(default=0)
    orphaned: Mapped[int] = mapped_column(default=0)
    report_path: Mapped[str | None]
