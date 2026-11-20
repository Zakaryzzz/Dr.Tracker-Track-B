import json
import sqlite3
import unittest
from copy import deepcopy
from contextlib import closing

from src.analysis import RAW, treatment_windows
from src.database import apply_schema, connect
from src.patient_summary import reports_to_clarify, score_rows


def report(key=11, text="My throat hurts."):
    return {"checkin_id": key, "submitted_at": "2026-01-02T08:00:00",
            "submission_date": "2026-01-02", "free_text": text,
            "raw_answers": "{}", "answers": {}}


def candidate(key=11, **changes):
    return {"checkin_id": key, "measure": "pain", "status": "present",
            "original_phrase": "My throat hurts.", **changes}


class ClarificationTests(unittest.TestCase):
    def test_unclassified_blank_and_source_filtering(self):
        source = report()
        before = deepcopy(source)
        rows = reports_to_clarify([source, report(99, " ")], [], [candidate(16)], {}, {})
        self.assertEqual(len(rows), 1)
        self.assertIn("Unclassified", rows[0]["reasons"])
        self.assertEqual(rows[0]["free_text"], before["free_text"])
        self.assertEqual(source, before)

    def test_partial_quote_does_not_clear_uncovered_text(self):
        text = "My throat hurts. I felt something strange afterward."
        rows = reports_to_clarify([report(text=text)], [], [candidate()], {}, {})
        self.assertIn("Coverage unconfirmed", rows[0]["reasons"])

    def test_failure_and_rejection_survive_successful_sibling(self):
        audit = {11: {"attempts": {"initial": {
            "valid": [candidate()], "rejected": [{"reason": "Nonliteral sibling"}]}}}}
        rows = reports_to_clarify([report()], [], [candidate()],
                                  {"errors": {11: "Bad output"}}, audit)
        self.assertIn("Extraction failed", rows[0]["reasons"])
        self.assertIn("Extraction needs checking", rows[0]["reasons"])
        self.assertIn("Nonliteral sibling", rows[0]["details"])

    def test_dedup_uncertainty_and_numeric_disagreement(self):
        source = report()
        source["raw_answers"] = '{"pain_score":0}'
        group = {"checkin_id": 11, "review_reasons": ["one_parser_only"],
                 "evidence": {"keyword": [candidate(status="uncertain", severity_score=3)],
                              "llm": [], "rejected_llm_observations": []}}
        rows = reports_to_clarify([source, source], [group, group], [], {}, {})
        self.assertEqual(len(rows), 1)
        self.assertIn("Uncertain", rows[0]["reasons"])
        self.assertIn("Different answers · timing unknown", rows[0]["reasons"])
        self.assertIn("Unmatched evidence", rows[0]["reasons"])

    def test_optional_rejected_fields_visible(self):
        row = candidate(rejected_fields={"severity_score": {"value": 9, "reason": "No scale"}})
        result = reports_to_clarify([report()], [], [], {"candidates": [row]}, {})
        self.assertIn("severity_score: No scale", result[0]["details"])

    def test_patient_source_and_raw_comparison_values(self):
        with closing(connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            apply_schema(db)
            db.execute("INSERT INTO patients VALUES (1,'Test','1980-01-01','Test','2026-01-01',NULL)")
            db.executemany(
                "INSERT INTO checkins (checkin_id,patient_id,submitted_at,raw_answers) "
                "VALUES (?,1,?,?)",
                [(11, "2026-01-01T08:00:00", '{"pain_score":0}'),
                 (12, "2026-01-03T08:00:00", '{"pain_score":4}')])
            db.execute("INSERT INTO treatments VALUES (41,1,NULL,'2026-01-02',2,'medication','Test','adjusted')")
            window = next(row for row in treatment_windows(db, 1, RAW) if row["measure"] == "pain")
            self.assertEqual((window["before_mean"], window["after_mean"]), (0, 4))
            empty = next(row for row in treatment_windows(db, 1) if row["measure"] == "pain")
            self.assertEqual((empty["before_mean"], empty["after_mean"]), (None, None))
            source = report()
            source["answers"] = {"pain_score": 0, "dizziness_score": "uncertain"}
            rows = score_rows(db, 1, RAW, [source])
            self.assertEqual(next(row["score"] for row in rows if row["measure"] == "pain"), 0)
            self.assertEqual(next(row["availability"] for row in rows if row["measure"] == "dizziness"), "Uncertain")
            self.assertEqual(next(row["score"] for row in rows if row["measure"] == "fatigue"), None)
