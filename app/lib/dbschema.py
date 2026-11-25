"""app/lib/dbschema.py — automatic column migration for applications.db (DATA-001).

The product's `applications` table lost three columns the old engine had: `worker`,
`model` (which AI ran the application) and `ats_host` (which application system
received it). Databases created before those columns were restored get them added
here at server start-up, one `ALTER TABLE ... ADD COLUMN` per missing column. Old
rows keep NULL (= unknown), so the migration never rewrites history — it only widens
the table. Idempotent: a DB that already has every expected column is untouched, and
a DB without the table (or a missing DB file) is not an error.
"""
import os
import sqlite3

# table -> columns the app expects to find there (all TEXT, all nullable).
EXPECTED = {"applications": ("worker", "model", "ats_host")}


def ensure_columns(db_path):
    """Add every EXPECTED column missing from its table (ALTER TABLE ADD COLUMN).

    Opens the DB read-write (ALTER needs it), adds each missing column as TEXT, and
    logs one line per added column. Never raises on a missing table or a missing DB
    file — a DB that doesn't exist yet was not created before this change, so there
    is nothing to migrate (and nothing to create here either).
    """
    if not os.path.isfile(db_path):
        return
    con = sqlite3.connect(db_path, timeout=5)
    try:
        for table, columns in EXPECTED.items():
            # PRAGMA table_info returns zero rows — without raising — when the table
            # does not exist; a real table always has at least one column. Either way
            # (missing table or a wonky DB file), there is nothing to migrate here.
            try:
                rows = con.execute("PRAGMA table_info({})".format(table)).fetchall()
            except sqlite3.Error:
                continue
            if not rows:
                continue
            present = {row[1] for row in rows}
            for col in columns:
                if col in present:
                    continue
                try:
                    con.execute("ALTER TABLE {} ADD COLUMN {} TEXT".format(table, col))
                except sqlite3.Error as exc:
                    # e.g. the DB is locked by a running apply — never a reason to stop the
                    # app; the column is added on the next start instead.
                    print("[db] {}: could not add column {} ({}); retrying on next start".format(
                        table, col, exc))
                    continue
                print("[db] {}: added column {}".format(table, col))
        con.commit()
    finally:
        con.close()
