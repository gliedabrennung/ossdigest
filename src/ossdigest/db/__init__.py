from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def run_migrations(conn: sqlite3.Connection) -> int:
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    files = _migration_files()

    applied = current_version
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version <= current_version:
            continue
        sql = f.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version={version}")
        applied = version

    return applied


def init_db(db_path: str | Path, *, database_url: str = ""):
    if database_url:
        from ossdigest.db.postgres_backend import init_db as init_db_postgres

        return init_db_postgres(database_url)

    conn = connect(db_path)
    run_migrations(conn)
    return conn
