"""Add named medication schedules to the existing synthetic P003 records."""
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .database import DATABASE_PATH, connect, ensure_medication_columns
from .demo_medications import MEDICATION_NAME, MEDICATION_SCHEDULES, MEDICATION_SOURCE
from .identifiers import identifier


def enrich(connection):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_medication_columns(connection)
        updated = 0
        for day, (status, frequency) in MEDICATION_SCHEDULES.items():
            event_id = identifier("treatments", f"demo-P003-{day:02d}-medication")
            row = connection.execute(
                "SELECT patient_id, event_type, treatment_day, event_status, treatment_name, "
                "medication_frequency, medication_details_source FROM treatments WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Missing synthetic medication event {event_id}")
            identity = (identifier("patients", "P003"), "medication", day, status)
            if row[:4] != identity:
                raise ValueError(f"Unexpected identity for medication event {event_id}")
            desired = (MEDICATION_NAME, frequency, MEDICATION_SOURCE)
            if row[4:] == desired:
                continue
            if row[4:] != ("Pain medication", None, None):
                raise ValueError(f"Medication event {event_id} was already edited; review before replacing it")
            connection.execute(
                "UPDATE treatments SET treatment_name = ?, medication_frequency = ?, "
                "medication_details_source = ? WHERE event_id = ?", (*desired, event_id),
            )
            updated += 1
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Foreign key check failed")
    return updated


def main():
    backup_dir = DATABASE_PATH.parent.parent / ".migration-backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"medication-details-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    with closing(connect()) as connection:
        with closing(sqlite3.connect(backup)) as backup_db:
            connection.backup(backup_db)
        print(f"Updated {enrich(connection)} synthetic medication events.")
        print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
