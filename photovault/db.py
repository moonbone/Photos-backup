"""Engine/session setup for the SQLite catalog."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .models import Base


def make_engine(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db(config: Config):
    """Create directories and tables; return a session factory."""
    config.ensure_dirs()
    engine = make_engine(config.db_path)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def open_session(config: Config) -> Session:
    return init_db(config)()
