"""Configuration loading (TOML)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "photovault" / "config.toml"
ENV_CONFIG = "PHOTOVAULT_CONFIG"
ENV_S3_KEY = "PHOTOVAULT_S3_KEY"
ENV_S3_SECRET = "PHOTOVAULT_S3_SECRET"


@dataclass
class StorageConfig:
    endpoint: str | None = None
    bucket: str = ""
    region: str | None = None
    access_key: str | None = None
    secret_key: str | None = None

    def resolved_credentials(self) -> tuple[str | None, str | None]:
        return (
            os.environ.get(ENV_S3_KEY) or self.access_key,
            os.environ.get(ENV_S3_SECRET) or self.secret_key,
        )


@dataclass
class SourceConfig:
    name: str
    staging: str
    kind: str = "folder"


@dataclass
class IngestConfig:
    extra_filename_date_patterns: list[str] = field(default_factory=list)


@dataclass
class VerifyConfig:
    sample_size: int = 50


@dataclass
class BrowseConfig:
    """Human-browsable local tree: date folders + capture-timestamp filename prefix."""

    enabled: bool = True
    path: str | None = None  # default: <library>/browse
    group_format: str = "%Y/%Y-%m-%d"  # strftime -> folder per day, inside a year folder
    prefix_format: str = "%Y%m%d_%H%M%S"  # strftime -> prepended so name-sort == time-sort
    link: str = "hardlink"  # hardlink | copy | symlink


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8420
    token: str | None = None
    upload_source: str = "uploads"


@dataclass
class Config:
    library_path: Path
    home_timezone: str = "UTC"
    storage: StorageConfig = field(default_factory=StorageConfig)
    sources: list[SourceConfig] = field(default_factory=list)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    web: WebConfig = field(default_factory=WebConfig)
    browse: BrowseConfig = field(default_factory=BrowseConfig)

    # -- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.library_path / "catalog.db"

    @property
    def objects_dir(self) -> Path:
        return self.library_path / "objects"

    @property
    def thumbs_dir(self) -> Path:
        return self.library_path / "thumbs"

    @property
    def previews_dir(self) -> Path:
        return self.library_path / "previews"

    @property
    def reports_dir(self) -> Path:
        return self.library_path / "reports"

    @property
    def browse_dir(self) -> Path:
        if self.browse.path:
            return Path(self.browse.path).expanduser()
        return self.library_path / "browse"

    @property
    def staging_root(self) -> Path:
        return self.library_path / "staging"

    def ensure_dirs(self) -> None:
        for d in (
            self.library_path,
            self.objects_dir,
            self.thumbs_dir,
            self.previews_dir,
            self.reports_dir,
            self.staging_root,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def source(self, name: str) -> SourceConfig | None:
        for s in self.sources:
            if s.name == name:
                return s
        return None


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or os.environ.get(ENV_CONFIG) or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {cfg_path}. Create one (see config.example.toml) "
            f"or pass --config / set ${ENV_CONFIG}."
        )
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)

    lib = data.get("library", {})
    storage = data.get("storage", {})
    cfg = Config(
        library_path=Path(lib.get("path", str(Path.home() / "photovault"))).expanduser(),
        home_timezone=lib.get("home_timezone", "UTC"),
        storage=StorageConfig(
            endpoint=storage.get("endpoint"),
            bucket=storage.get("bucket", ""),
            region=storage.get("region"),
            access_key=storage.get("access_key"),
            secret_key=storage.get("secret_key"),
        ),
        sources=[
            SourceConfig(name=s["name"], staging=s["staging"], kind=s.get("kind", "folder"))
            for s in data.get("sources", [])
        ],
        ingest=IngestConfig(
            extra_filename_date_patterns=data.get("ingest", {}).get(
                "extra_filename_date_patterns", []
            )
        ),
        verify=VerifyConfig(sample_size=data.get("verify", {}).get("sample_size", 50)),
        web=WebConfig(
            host=data.get("web", {}).get("host", "127.0.0.1"),
            port=data.get("web", {}).get("port", 8420),
            token=data.get("web", {}).get("token"),
            upload_source=data.get("web", {}).get("upload_source", "uploads"),
        ),
        browse=BrowseConfig(
            enabled=data.get("browse", {}).get("enabled", True),
            path=data.get("browse", {}).get("path"),
            group_format=data.get("browse", {}).get("group_format", "%Y/%Y-%m-%d"),
            prefix_format=data.get("browse", {}).get("prefix_format", "%Y%m%d_%H%M%S"),
            link=data.get("browse", {}).get("link", "hardlink"),
        ),
    )
    return cfg
