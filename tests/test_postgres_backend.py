from __future__ import annotations

from ossdigest.db.postgres_backend import PgConnection, translate_placeholders


def test_translate_placeholders_converts_qmark_to_psycopg_style():
    assert translate_placeholders("SELECT * FROM t WHERE a = ? AND b = ?") == "SELECT * FROM t WHERE a = %s AND b = %s"
    assert translate_placeholders("SELECT 1") == "SELECT 1"


class _FakeCursor:
    def __init__(self):
        self.executed_sql: str | None = None
        self.executed_params: tuple | None = None

    def execute(self, sql, params):
        self.executed_sql = sql
        self.executed_params = params

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRawConnection:
    def __init__(self):
        self.autocommit = False
        self.last_cursor: _FakeCursor | None = None

    def cursor(self, row_factory=None):
        self.last_cursor = _FakeCursor()
        return self.last_cursor


def test_pgconnection_execute_translates_placeholders_and_sets_autocommit():
    raw = _FakeRawConnection()
    conn = PgConnection(raw)

    assert raw.autocommit is True

    conn.execute("SELECT * FROM repos WHERE id = ?", (42,))

    assert raw.last_cursor.executed_sql == "SELECT * FROM repos WHERE id = %s"
    assert raw.last_cursor.executed_params == (42,)
