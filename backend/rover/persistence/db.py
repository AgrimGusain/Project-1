"""Database engine and versioned schema migrations.

Schema strategy: an ordered list of migrations, each with an integer version.
Applied versions are recorded in ``schema_version``. Never edit a released
migration; append a new one (e.g. ``ALTER TABLE ... ADD COLUMN``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Connection, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rover.persistence.models import Base, SchemaVersion

log = logging.getLogger(__name__)


def _v1_initial(conn: Connection) -> None:
    Base.metadata.create_all(conn)


MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "initial schema", _v1_initial),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        kwargs: dict[str, object] = {}
        if url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool  # one shared connection, otherwise each gets an empty DB
        self.engine: Engine = create_engine(url, connect_args=connect_args, future=True, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.write_lock = threading.RLock()

    @staticmethod
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @classmethod
    def for_path(cls, path: str) -> "Database":
        if path == ":memory:":
            return cls("sqlite+pysqlite:///:memory:")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return cls(f"sqlite+pysqlite:///{p.as_posix()}")

    def session(self) -> Session:
        return self._sessions()

    def migrate(self) -> int:
        with self.engine.begin() as conn:
            if not inspect(conn).has_table("schema_version"):
                SchemaVersion.__table__.create(conn)
            applied = {row[0] for row in conn.execute(text("SELECT version FROM schema_version"))}
            for version, description, fn in MIGRATIONS:
                if version in applied:
                    continue
                log.info("applying schema migration v%s: %s", version, description)
                fn(conn)
                conn.execute(
                    text("INSERT INTO schema_version (version, applied_at, description) VALUES (:v, :t, :d)"),
                    {"v": version, "t": _naive_utc().isoformat(sep=" "), "d": description},
                )
            current = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar() or 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{current} is newer than this software (v{SCHEMA_VERSION})")
        return int(current)

    def close(self) -> None:
        self.engine.dispose()
