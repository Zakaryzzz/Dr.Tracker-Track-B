"""Initialize or verify the Dr.Tracker SQLite schema."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_DIR / "data" / "drtracker.db"
SCHEMA_PATH = PROJECT_DIR / "schema.sql"

# Each table's primary key and its expected foreign key column mappings.
TABLE_KEYS = {
    "patients": ("patient_id", set()),
    "checkins": ("checkin_id", {("patients", "patient_id", "patient_id")}),
    "symptoms": ("observation_id", {
        ("checkins", "checkin_id", "checkin_id"),
        ("treatments", "related_event_id", "event_id"),
    }),
    "weight": ("weight_id", {
        ("patients", "patient_id", "patient_id"),
        ("checkins", "checkin_id", "checkin_id"),
        ("checkins", "patient_id", "patient_id"),
    }),
    "treatments": ("event_id", {
        ("patients", "patient_id", "patient_id"),
        ("checkins", "checkin_id", "checkin_id"),
        ("checkins", "patient_id", "patient_id"),
    }),
}


def connect(path=DATABASE_PATH, *, uri=False):
    """Open a connection with foreign key enforcement enabled."""
    connection = sqlite3.connect(path, uri=uri)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        require(connection.execute("PRAGMA foreign_keys").fetchone() == (1,),
                "Foreign key enforcement")
    except Exception:
        connection.close()
        raise
    return connection


def apply_schema(connection):
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise RuntimeError("SQLite 3.37 or newer is required by this project.")
    try:
        connection.execute("SELECT json_valid('{}')").fetchone()
    except sqlite3.OperationalError as error:
        raise RuntimeError("This SQLite build must provide json_valid().") from error
    existing = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(patients)")}
    if existing and (existing.get("patient_id") != "INTEGER" or existing.get("date_of_birth") != "DATE"):
        raise RuntimeError("Legacy database detected. Run python -m src.migrate_schema first.")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def table_names(connection):
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table'"
    )}


def require(condition, label):
    if not condition:
        raise AssertionError(label)


def expect_rejection(connection, label, sql, parameters, expected_code):
    try:
        connection.execute(sql, parameters)
    except sqlite3.IntegrityError as error:
        require(error.sqlite_errorname == expected_code, f"{label}: {error}")
        print(f"PASS: {label}")
    else:
        raise AssertionError(f"{label}: insert unexpectedly succeeded")


def verify_schema():
    with closing(connect(":memory:")) as connection:
        apply_schema(connection)
        require(table_names(connection) == TABLE_KEYS.keys(), "Five expected tables")
        print("PASS: five expected tables and foreign key enforcement enabled")
        for table, (primary_key, foreign_keys) in TABLE_KEYS.items():
            # Table names come from the fixed TABLE_KEYS mapping above.
            columns = connection.execute(f"PRAGMA table_info({table})")
            require([row[1] for row in columns if row[5]] == [primary_key],
                    f"{table} primary key")
            keys = connection.execute(f"PRAGMA foreign_key_list({table})")
            require({(row[2], row[3], row[4]) for row in keys} == foreign_keys,
                    f"{table} foreign keys")
        print("PASS: primary and foreign key definitions")

        connection.executemany(
            "INSERT INTO patients VALUES (?, 'Test', '1980-01-01', 'Test', '2026-01-01', NULL)",
            [(1,), (2,)],
        )
        insert_checkin = """
            INSERT INTO checkins (checkin_id, patient_id, submitted_at, raw_answers)
            VALUES (?, ?, '2026-01-01T12:00:00', ?)
        """
        connection.executemany(insert_checkin, [(11, 1, "{}"), (12, 1, "{}")])
        require(connection.execute("SELECT count(*) FROM checkins").fetchone() == (2,),
                "Same-day check-ins")
        print("PASS: multiple same-day check-ins")

        insert_symptom = """
            INSERT INTO symptoms (
                observation_id, checkin_id, measure, status, severity_score,
                source_type, extraction_method, review_status, validation_status
            ) VALUES (31, 11, 'test', 'uncertain', ?, 'questionnaire',
                      'questionnaire', 'pending', 'pending')
        """
        for severity in (-1, 11):
            expect_rejection(connection, f"Severity {severity}", insert_symptom,
                             (severity,), "SQLITE_CONSTRAINT_CHECK")
        insert_weight = "INSERT INTO weight VALUES (21, ?, 11, '2026-01-01', ?)"
        expect_rejection(connection, "Negative weight", insert_weight,
                         (1, -1), "SQLITE_CONSTRAINT_CHECK")
        expect_rejection(connection, "Invalid JSON", insert_checkin,
                         (13, 1, "{"), "SQLITE_CONSTRAINT_CHECK")
        expect_rejection(connection, "Missing patient reference", insert_checkin,
                         (13, 999, "{}"), "SQLITE_CONSTRAINT_FOREIGNKEY")
        expect_rejection(connection, "Weight check-in belongs to another patient",
                         insert_weight, (2, 150), "SQLITE_CONSTRAINT_FOREIGNKEY")
        expect_rejection(
            connection, "Treatment check-in belongs to another patient",
            "INSERT INTO treatments VALUES (41, 2, 11, '2026-01-01', 1, 'test', 'test', 'test')",
            (), "SQLITE_CONSTRAINT_FOREIGNKEY",
        )
        connection.commit()
        apply_schema(connection)
        require(connection.execute("SELECT count(*) FROM checkins").fetchone() == (2,),
                "Reinitialization preserves records")
        print("PASS: reinitialization preserves existing records")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Verify using an in-memory database")
    args = parser.parse_args()

    if args.verify:
        verify_schema()
        return

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as connection:
        apply_schema(connection)
        print(f"Database: {DATABASE_PATH}")
        print("Foreign keys: ON")
        print("Tables:", ", ".join(sorted(table_names(connection))))


if __name__ == "__main__":
    main()
