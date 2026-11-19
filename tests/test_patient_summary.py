import json
import sqlite3
import unittest

from src.analysis import RAW, VALIDATED
from src.database import apply_schema, connect
from src.patient_summary import (
    daily_scores, medication_events, patient_reports, recent_evidence,
    score_rows, trend_summary, visit_comparison, vomiting_summary,
)


class PatientSummaryTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        apply_schema(self.db)
        self.db.execute("INSERT INTO patients VALUES ('p1','Test','1980-01-01','Test','2026-01-01',NULL)")
        self.db.execute("INSERT INTO patients VALUES ('p2','Other','1980-01-01','Test','2026-01-01',NULL)")
        for day in range(1, 15):
            answers = {"pain_score": 0 if day <= 7 else 4,
                       "vomiting_count": None if day == 14 else "uncertain" if day == 13 else 1}
            self.db.execute(
                "INSERT INTO checkins (checkin_id,patient_id,submitted_at,treatment_day,raw_answers,free_text)"
                " VALUES (?, 'p1', ?, ?, ?, ?)",
                (f"c{day}", f"2026-01-{day:02d}T08:00:00", day,
                 json.dumps(answers), "My throat hurts."),
            )

    def test_daily_window_rule_and_null_preservation(self):
        reports = patient_reports(self.db, "p1")
        scores = score_rows(self.db, "p1", RAW, reports)
        state, change = trend_summary(scores, "2026-01-14")
        self.assertEqual(state, "Worsening")
        self.assertEqual((change["before"], change["after"], change["before_n"], change["after_n"]),
                         (0, 4, 7, 7))
        validated = score_rows(self.db, "p1", VALIDATED, reports)
        self.assertTrue(all(r["score"] is None for r in validated))
        self.assertEqual(trend_summary(validated, "2026-01-14"), ("Insufficient Data", None))
        self.assertEqual(patient_reports(self.db, "p2"), [])

    def test_rolling_counts_include_uncertain_and_missing(self):
        result = vomiting_summary(patient_reports(self.db, "p1"))
        self.assertEqual((result["start"], result["end"]), ("2026-01-08", "2026-01-14"))
        self.assertEqual((result["sum"], result["numeric"], result["missing"], result["uncertain"]),
                         (5, 5, 1, 1))

    def test_latest_daily_submission_keeps_missing(self):
        rows = [dict(submission_date="2026-01-01", measure="pain", score=3,
                     submitted_at="2026-01-01T08:00:00", checkin_id="a"),
                dict(submission_date="2026-01-01", measure="pain", score=None,
                     submitted_at="2026-01-01T20:00:00", checkin_id="b")]
        self.assertIsNone(daily_scores(rows)[0]["score"])

    def test_visit_baseline_and_fallback_are_explicit(self):
        reports = patient_reports(self.db, "p1")
        scores = score_rows(self.db, "p1", RAW, reports)
        context, _ = visit_comparison(self.db, "p1", reports, scores)
        self.assertIn("two latest submissions", context)
        self.db.execute("INSERT INTO treatments VALUES ('visit','p1',NULL,'2026-01-06',6,'follow_up','Visit','completed')")
        context, changes = visit_comparison(self.db, "p1", reports, scores)
        self.assertIn("2026-01-06T08:00:00", context)
        self.assertIn("Pain 0/10 → 4/10 (+4).", changes)

    def test_recent_text_retains_review_and_provenance(self):
        reports = patient_reports(self.db, "p1")
        group = {"checkin_id":"c14", "measure":"pain", "review_status":"needs_review",
                 "evidence":{"keyword":[{"measure":"pain","status":"uncertain","severity_score":None,
                                         "frequency_count":None}], "llm":[]}}
        recent = recent_evidence(reports, [group])
        self.assertEqual(recent[0]["Check-in"], "c14")
        self.assertEqual(recent[0]["Original patient statement"], "My throat hurts.")
        self.assertIn("keyword: pain (uncertain)", recent[0]["Extracted symptoms"])
        self.assertIn("needs review", recent[0]["Uncertainty / validation"])
        self.assertEqual(medication_events(self.db, "p2"), [])
