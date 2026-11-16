import sqlite3
import unittest
from contextlib import closing

from src.analysis import RAW, VALIDATED, daily_weight, open_readonly, pain_trend, treatment_windows, weekly_vomiting
from src.database import apply_schema, connect


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        apply_schema(self.db)
        for patient in ("p1", "p2"):
            self.db.execute("INSERT INTO patients VALUES (?, 'Test', '1980-01-01', 'Test', '2026-01-01', NULL)",
                            (patient,))
        self.db.executemany(
            "INSERT INTO checkins (checkin_id, patient_id, submitted_at, raw_answers) VALUES (?, ?, ?, ?)",
            [
                ("a", "p1", "2026-01-04T08:00:00", '{"vomiting_count":2,"pain_score":0}'),
                ("b", "p1", "2026-01-05T08:00:00", '{"vomiting_count":0,"pain_score":4}'),
                ("c", "p1", "2026-01-05T20:00:00", '{"vomiting_count":"uncertain"}'),
                ("d", "p1", "2026-01-06T08:00:00", '{"vomiting_count":null}'),
                ("e", "p1", "2026-01-12T08:00:00", '{}'),
                ("other", "p2", "2026-01-05T08:00:00", '{"vomiting_count":9}'),
            ],
        )
        self.db.executemany(
            "INSERT INTO symptoms (observation_id, checkin_id, measure, status, severity_score, "
            "source_type, extraction_method, review_status, validation_status) "
            "VALUES (?, ?, 'pain', ?, ?, 'questionnaire', 'questionnaire', 'pending', ?)",
            [("s1", "a", "absent", 0, "passed"), ("s2", "b", "present", 4, "pending")],
        )

    def test_week_boundaries_response_categories_and_patient_filter(self):
        rows = weekly_vomiting(self.db, "p1")
        columns = ("week_start", "positive", "explicit_zero", "uncertain", "missing",
                   "reported_count_sum", "submissions")
        self.assertEqual([tuple(row[key] for key in columns) for row in rows], [
            ("2025-12-29", 1, 0, 0, 0, 2, 1),
            ("2026-01-05", 0, 1, 1, 1, 0, 3),
            ("2026-01-12", 0, 0, 0, 1, None, 1),
        ])
        self.assertEqual(weekly_vomiting(self.db, "p2")[0]["reported_count_sum"], 9)

    def test_layers_preserve_zero_and_prevent_duplicate_joins(self):
        self.assertEqual([row["pain_score"] for row in pain_trend(self.db, "p1", RAW)],
                         [0, 4, None, None, None])
        self.assertEqual([row["pain_score"] for row in pain_trend(self.db, "p1", VALIDATED)],
                         [0, None, None, None, None])
        self.db.execute(
            "INSERT INTO symptoms SELECT 'duplicate', checkin_id, measure, status, severity_score, "
            "frequency_count, frequency_period, value_text, original_phrase, observed_date, "
            "observed_time_text, source_type, pain_location, related_event_id, extraction_method, "
            "possible_duplicate_group, review_status, review_reason, validation_status "
            "FROM symptoms WHERE observation_id='s1'"
        )
        self.assertEqual([row["pain_score"] for row in pain_trend(self.db, "p1", VALIDATED)], [None] * 5)
        self.assertEqual(sum(row["submissions"] for row in weekly_vomiting(self.db, "p1")), 5)

    def test_treatment_windows_and_insufficient_data(self):
        self.db.execute("INSERT INTO treatments VALUES ('event', 'p1', NULL, '2026-01-06', NULL, 'medication', 'Test', 'adjusted')")
        row = next(row for row in treatment_windows(self.db, "p1") if row["measure"] == "pain")
        self.assertEqual((row["before_submissions"], row["after_submissions"]), (2, 1))
        self.assertEqual((row["before_scores"], row["after_scores"]), (1, 0))
        self.assertEqual((row["before_mean"], row["after_mean"]), (0, None))
        self.assertEqual(row["coverage"], "Insufficient data")
        self.assertEqual(treatment_windows(self.db, "p2"), [])

    def test_weight_uses_measurement_date_and_latest_submission(self):
        self.db.executemany("INSERT INTO weight VALUES (?, 'p1', ?, '2026-01-04', ?)", [
            ("w1", "b", 150), ("w2", "c", 149), ("w3", "c", 148),
        ])
        row, = daily_weight(self.db, "p1")
        self.assertEqual((row["measured_date"], row["weight_id"], row["weight_lb"],
                          row["measurements_that_day"]), ("2026-01-04", "w3", 148, 3))
        self.assertEqual(self.db.execute("SELECT count(*) FROM weight").fetchone()[0], 3)

    def test_dashboard_database_is_read_only(self):
        with closing(open_readonly()) as db:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("UPDATE patients SET patient_name = patient_name")
