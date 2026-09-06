from __future__ import annotations

import sys
from datetime import datetime, timezone

from ossdigest.config import load_secrets
from ossdigest.db import connect
from ossdigest.state import get_state

MAX_STALE_SECONDS = 180


def main() -> int:
    secrets = load_secrets()
    conn = connect(secrets.database_path)
    value = get_state(conn, "publisher_heartbeat")
    conn.close()

    if value is None:
        print("no heartbeat recorded yet", file=sys.stderr)
        return 1

    last = datetime.fromisoformat(value)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()

    if age > MAX_STALE_SECONDS:
        print(f"heartbeat stale: {age:.0f}s old", file=sys.stderr)
        return 1

    print(f"ok: heartbeat {age:.0f}s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())
