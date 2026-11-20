"""Back up and migrate the local database to numeric keys and declared dates."""
import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from .database import DATABASE_PATH, SCHEMA_PATH, TABLE_KEYS
from .identifiers import normalize_identifiers


def migrate(path=DATABASE_PATH):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        types = {row["name"]: row["type"] for row in source.execute("PRAGMA table_info(patients)")}
        if types.get("patient_id") == "INTEGER" and types.get("date_of_birth") == "DATE":
            return {"status": "already_migrated"}
        if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Resolve existing foreign-key violations before migrating")
        backup_dir = path.parent.parent / ".migration-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        with closing(sqlite3.connect(backup)) as backup_db:
            source.backup(backup_db)
        rows = {
            table: [normalize_identifiers(dict(row)) for row in source.execute(f"SELECT * FROM {table}")]
            for table in TABLE_KEYS
        }
    before = sha256(path.read_bytes()).hexdigest()
    with TemporaryDirectory(dir=path.parent) as directory:
        target = Path(directory) / "migrated.db"
        with closing(sqlite3.connect(target)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            with db:
                for table in ("patients", "checkins", "treatments", "symptoms", "weight"):
                    for row in rows[table]:
                        columns = ", ".join(row)
                        placeholders = ", ".join("?" for _ in row)
                        db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(row.values()))
                for table, (key, _) in TABLE_KEYS.items():
                    expected = {row[key]: row for row in rows[table]}
                    if len(expected) != len(rows[table]):
                        raise ValueError(f"Numeric identifier collision in {table}")
                    actual = {row[key]: dict(row) for row in db.execute(f"SELECT * FROM {table}")}
                    if expected != actual:
                        raise ValueError(f"Content differs after migration for {table}")
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Migrated foreign keys are invalid")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Migrated database integrity failed")
        if sha256(path.read_bytes()).hexdigest() != before:
            raise ValueError("Source database changed during migration; rerun with writers stopped")
        os.replace(target, path)
    return {"status": "migrated", "backup": str(backup),
            "rows": {table: len(records) for table, records in rows.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(migrate())


if __name__ == "__main__":
    main()
