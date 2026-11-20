import sqlite3
import unittest

from src.database import apply_schema, connect
from src.extract_symptoms import map_questionnaire
from src.insert_symptoms import process
from src.reconcile_phase3c import compare_group


class InsertionTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        apply_schema(self.db)
        self.raw = '{"pain_score":0,"vomiting_count":0,"dizziness_score":"uncertain","fever":null}'
        with self.db:
            self.db.execute(
                "INSERT INTO patients VALUES (1, 'Test', '1980-01-01', 'Test', '2026-01-01', NULL)"
            )
            self.db.execute(
                "INSERT INTO checkins (checkin_id, patient_id, submitted_at, raw_answers) "
                "VALUES (11, 1, '2026-01-01T12:00:00', ?)", (self.raw,)
            )
        self.groups = []
        for answer in map_questionnaire(11, self.raw):
            group = compare_group(11, answer["measure"], "", [answer], [], [], [], False, [])
            group.update(insertion_candidates=group["questionnaire_observations"],
                         insertion_eligibility="eligible", review_status="pending")
            self.groups.append(group)
        self.report = {"results": self.groups}

    def test_insertion_preserves_zero_uncertainty_and_null(self):
        self.assertEqual(process(self.db, self.report)["inserted"], 3)
        self.assertEqual(self.db.execute(
            "SELECT measure, status, severity_score, frequency_count, observed_date, "
            "observed_time_text, original_phrase, review_status, validation_status "
            "FROM symptoms ORDER BY measure"
        ).fetchall(), [
            ("dizziness", "uncertain", None, None, None, None, None, "pending", "passed"),
            ("pain", "absent", 0, None, None, None, None, "pending", "passed"),
            ("vomiting", "absent", None, 0, None, None, None, "pending", "passed"),
        ])

    def test_dry_run_verify_and_repeat(self):
        self.assertEqual(process(self.db, self.report, "dry-run")["would_insert"], 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM symptoms").fetchone(), (0,))
        with self.assertRaisesRegex(ValueError, "missing"):
            process(self.db, self.report, "verify")
        process(self.db, self.report)
        self.assertEqual(process(self.db, self.report, "verify")["foreign_key_check"], "passed")
        repeat = process(self.db, self.report)
        self.assertEqual((repeat["inserted"], repeat["already_identical"]), (0, 3))

    def test_foreign_keys_and_patient_association(self):
        self.db.execute("PRAGMA foreign_keys = OFF")
        with self.db:
            self.db.execute("DELETE FROM patients")
        with self.assertRaisesRegex(ValueError, "disabled"):
            process(self.db, self.report)
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.assertRaisesRegex(ValueError, "foreign key check failed"):
            process(self.db, self.report)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM symptoms").fetchone(), (0,))

    def test_invalid_severity_frequency_date_and_identity_rejected(self):
        candidate = self.groups[0]["insertion_candidates"][0]
        for field, value in (("severity_score", 11), ("severity_score", False),
                             ("frequency_count", -1), ("observed_date", "2026-01-01"),
                             ("frequency_period", "today"), ("observation_id", "different")):
            with self.subTest(field=field, value=value):
                previous = candidate[field]
                candidate[field] = value
                with self.assertRaisesRegex(ValueError, "does not match"):
                    process(self.db, self.report)
                candidate[field] = previous
                self.assertEqual(self.db.execute("SELECT COUNT(*) FROM symptoms").fetchone(), (0,))

    def test_conflicting_id_preserves_existing_row(self):
        process(self.db, {"results": [self.groups[-1]]})
        with self.db:
            self.db.execute("UPDATE symptoms SET frequency_count = 1")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            process(self.db, self.report)
        self.assertEqual(self.db.execute("SELECT frequency_count FROM symptoms").fetchall(), [(1,)])

    def test_database_insert_failure_rolls_back_prior_rows(self):
        self.db.execute(
            "CREATE TRIGGER reject_last BEFORE INSERT ON symptoms "
            "WHEN NEW.measure = 'vomiting' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test failure"):
            process(self.db, self.report)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM symptoms").fetchone(), (0,))

    def test_excludes_unresolved_groups(self):
        held = self.groups.pop()
        # The original answer and evidence stay available for human review.
        held.update(insertion_candidates=[], insertion_eligibility="held_for_review",
                    review_reasons=["questionnaire_time_unknown"], text_parser_status="agreement")
        self.groups.append(held)
        result = process(self.db, self.report)
        self.assertEqual((result["inserted"], result["excluded_review_groups"]), (2, 1))
        self.assertIsNone(self.db.execute("SELECT 1 FROM symptoms WHERE measure='vomiting'").fetchone())
