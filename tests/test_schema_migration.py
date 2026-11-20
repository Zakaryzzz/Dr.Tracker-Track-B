import json
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from src.database import apply_schema, connect
from src.identifiers import identifier, normalize_identifiers
from src.migrate_schema import migrate

LEGACY = """
CREATE TABLE patients (
 patient_id TEXT PRIMARY KEY, patient_name TEXT, date_of_birth TEXT,
 diagnosis TEXT, treatment_start_date TEXT, weight_threshold_lb REAL);
CREATE TABLE checkins (
 checkin_id TEXT PRIMARY KEY, patient_id TEXT, submitted_at TEXT, treatment_day INTEGER,
 raw_answers TEXT, free_text TEXT, photo_reference TEXT);
CREATE TABLE symptoms (
 observation_id TEXT PRIMARY KEY, checkin_id TEXT, measure TEXT, status TEXT,
 severity_score INTEGER, frequency_count INTEGER, frequency_period TEXT, value_text TEXT,
 original_phrase TEXT, observed_date TEXT, observed_time_text TEXT, source_type TEXT,
 pain_location TEXT, related_event_id TEXT, extraction_method TEXT,
 possible_duplicate_group TEXT, review_status TEXT, review_reason TEXT, validation_status TEXT);
CREATE TABLE weight (
 weight_id TEXT PRIMARY KEY, patient_id TEXT, checkin_id TEXT, measured_date TEXT, weight_lb REAL);
CREATE TABLE treatments (
 event_id TEXT PRIMARY KEY, patient_id TEXT, checkin_id TEXT, event_date TEXT,
 treatment_day INTEGER, event_type TEXT, treatment_name TEXT, event_status TEXT);
"""


class NumericSchemaTests(unittest.TestCase):
    def test_numeric_types_and_calendar_validation(self):
        with closing(connect(":memory:")) as db:
            apply_schema(db)
            self.assertEqual(dict((r[1], r[2]) for r in db.execute("PRAGMA table_info(patients)"))["patient_id"], "INTEGER")
            self.assertEqual(dict((r[1], r[2]) for r in db.execute("PRAGMA table_info(patients)"))["date_of_birth"], "DATE")
            for bad_date in ("2026-02-30", "2026-13-01", "today", "2026-1-01", "12345"):
                with self.subTest(date=bad_date), self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO patients VALUES (1,'Test',?,'Test','2026-01-01',NULL)", (bad_date,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO patients VALUES ('P001','Test','1980-01-01','Test','2026-01-01',NULL)")
            db.execute("INSERT INTO patients VALUES (1,'Test','1980-01-01','Test','2026-01-01',NULL)")
            for timestamp in ("2026-02-30T12:00:00", "2026-01-01", "2026-01-01T25:00:00"):
                with self.subTest(timestamp=timestamp), self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO checkins VALUES (1,1,?,1,'{}',NULL,NULL)", (timestamp,))

    def test_legacy_json_indexes_and_evidence_remain_compatible(self):
        key = "demo-P001-01-1"
        evidence = f"Example mentions {key} in the original text."
        original = {"checkin_id": key, "original_phrase": evidence, "errors": {key: "Example"},
                    "processed_checkin_ids": [key]}
        migrated = normalize_identifiers(original)
        value = identifier("checkins", key)
        self.assertIs(type(value), int)
        self.assertEqual(migrated["checkin_id"], value)
        self.assertEqual(migrated["errors"], {value: "Example"})
        self.assertEqual(migrated["original_phrase"], evidence)
        self.assertEqual(normalize_identifiers(json.loads(json.dumps(migrated))), migrated)
        self.assertEqual(original["checkin_id"], key)

    def legacy_database(self, root, birth="1980-01-01"):
        folder = root / "data"
        folder.mkdir()
        path = folder / "test.db"
        with closing(sqlite3.connect(path)) as db:
            db.executescript(LEGACY)
            with db:
                db.execute("INSERT INTO patients VALUES ('P001','Test',?,'Test','2026-01-01',NULL)", (birth,))
                db.execute("INSERT INTO checkins VALUES ('demo-P001-01-1','P001','2026-01-01T08:00:00',1,'{}','Original text',NULL)")
        return path

    def test_backed_up_migration_preserves_content_and_is_repeatable(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = self.legacy_database(Path(folder))
            result = migrate(path)
            self.assertTrue(Path(result["backup"]).is_file())
            with closing(connect(path)) as db:
                self.assertEqual(db.execute("SELECT typeof(patient_id), patient_name FROM patients").fetchone(),
                                 ("integer", "Test"))
                self.assertEqual(db.execute("SELECT free_text FROM checkins").fetchone()[0], "Original text")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            before = sha256(path.read_bytes()).hexdigest()
            self.assertEqual(migrate(path)["status"], "already_migrated")
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), before)

    def test_invalid_legacy_date_leaves_original_database_intact(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = self.legacy_database(Path(folder), "2026-02-30")
            before = sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(sqlite3.IntegrityError):
                migrate(path)
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), before)
