"""Read-only: the database file size and each table's rows and space, largest first (changes nothing).

    ./docker.sh db-size"""

import os
import sqlite3

from backend.database.models import db_path


def main() -> None:
    path = str(db_path())
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    total = os.path.getsize(path) + sum(os.path.getsize(path + x) for x in ("-wal", "-shm") if os.path.exists(path + x))
    free = conn.execute("PRAGMA freelist_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    print(f"\n{path}: {total / 1e6:,.1f} MB on disk ({free / 1e6:,.1f} MB free inside, reusable)\n")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    try:
        size = dict(conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall())
    except sqlite3.OperationalError:                  # SQLite built without dbstat: rows only
        size = {}
    rows = []
    for t in tables:
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        idx = sum(size.get(i[0], 0) for i in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (t,)))
        rows.append((size.get(t, 0) + idx, n, t))
    print(f"{'table':<26}{'rows':>12}{'MB':>10}")
    for b, n, t in sorted(rows, reverse=True):
        print(f"{t:<26}{n:>12,}{b / 1e6:>10.1f}")


if __name__ == "__main__":
    main()
