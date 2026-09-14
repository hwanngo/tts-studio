"""SQLite connection and migration boundary owned by the Core."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tts_studio.storage.layout import UnsafeStoragePathError

_LATEST_SCHEMA_VERSION = 13


class UnsupportedDatabaseVersionError(RuntimeError):
    """Raised when a database is newer than this Core understands."""


class Database:
    """Open configured SQLite connections and serialize all Core write transactions."""

    def __init__(self, path: Path, migrations_directory: Path | None = None) -> None:
        self.path = path.expanduser().absolute()
        self._migrations_directory = migrations_directory or Path(__file__).with_name("migrations")

    def migrate(self) -> None:
        """Apply every newer numbered migration atomically and in order."""
        with self._connection() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > _LATEST_SCHEMA_VERSION:
                raise UnsupportedDatabaseVersionError(
                    f"database schema {current} is newer than supported schema "
                    f"{_LATEST_SCHEMA_VERSION}"
                )

            for migration in self._migration_files():
                version = int(migration.name.split("_", maxsplit=1)[0])
                if version <= current:
                    continue
                sql = migration.read_text(encoding="utf-8")
                try:
                    connection.executescript(
                        f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                current = version

            if current != _LATEST_SCHEMA_VERSION:
                raise UnsupportedDatabaseVersionError(
                    f"database schema stopped at {current}; expected {_LATEST_SCHEMA_VERSION}"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield one explicit write transaction with foreign keys enabled."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Yield one consistent read snapshot configured like write connections."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                connection.rollback()

    def _migration_files(self) -> tuple[Path, ...]:
        migrations = tuple(sorted(self._migrations_directory.glob("[0-9][0-9][0-9]_*.sql")))
        versions = [int(path.name.split("_", maxsplit=1)[0]) for path in migrations]
        if versions != sorted(set(versions)):
            raise RuntimeError("migration versions must be unique and ordered")
        if versions != list(range(2, _LATEST_SCHEMA_VERSION + 1)):
            raise RuntimeError("migration versions must be contiguous")
        return migrations

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise UnsafeStoragePathError("SQLite database path cannot be a symbolic link")
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()
