from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).parent / "migrations_postgres"


def translate_placeholders(sql: str) -> str:
    return sql.replace("?", "%s")


class PgCursor:
    def __init__(self, raw_cursor: psycopg.Cursor) -> None:
        self._raw = raw_cursor

    def fetchone(self) -> dict[str, Any] | None:
        return self._raw.fetchone()

    def fetchall(self) -> list[dict[str, Any]]:
        return self._raw.fetchall()

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount


class PgConnection:
    def __init__(self, raw_conn: psycopg.Connection) -> None:
        self._raw = raw_conn
        self._raw.autocommit = True

    def execute(self, sql: str, params: tuple = ()) -> PgCursor:
        cur = self._raw.cursor(row_factory=dict_row)
        cur.execute(translate_placeholders(sql), params)
        return PgCursor(cur)

    def executescript(self, sql: str) -> None:
        with self._raw.cursor() as cur:
            cur.execute(sql)

    def close(self) -> None:
        self._raw.close()


def connect(database_url: str) -> PgConnection:
    return PgConnection(psycopg.connect(database_url))


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def run_migrations(conn: PgConnection) -> int:
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    current_version = row["version"] if row else 0

    applied = current_version
    for f in _migration_files():
        version = int(f.name.split("_", 1)[0])
        if version <= current_version:
            continue
        sql = f.read_text(encoding="utf-8")
        conn.executescript(sql)
        applied = version

    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (applied,))
    elif applied != current_version:
        conn.execute("UPDATE schema_version SET version = ?", (applied,))

    return applied


def init_db(database_url: str) -> PgConnection:
    conn = connect(database_url)
    run_migrations(conn)
    return conn
