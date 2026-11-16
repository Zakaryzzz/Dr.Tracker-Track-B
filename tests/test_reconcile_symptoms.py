import json
import unittest
from contextlib import closing
from unittest.mock import patch

from src.database import DATABASE_PATH, connect
from src.extract_symptoms import parse_keywords
from src.llm_extraction import DEFAULT_MODEL, extract_live, validate_response
from src.reconcile_symptoms import reconcile

REPORT = "I threw up twice this afternoon."
SUBMITTED = "2026-09-24T20:00:00"


def fixture(**changes):
    item = dict(measure="vomiting", status="present", severity_score=None,
                frequency_count=2, frequency_period=None, pain_location=None,
                observed_date=None, observed_time_text="this afternoon",
                original_phrase=REPORT)
    item.update(changes)
    return json.dumps({"observations": [item]})


def compare(*, report=REPORT, model=None, questionnaire=(), available=True):
    if model is None:
        model = validate_response(fixture(), "test", report)
    return reconcile("test", SUBMITTED, report, questionnaire, parse_keywords("test", report),
                     model, llm_available=available)[0]


class Phase3BTests(unittest.TestCase):
    def test_structured_json_and_invalid_fields(self):
        row, = validate_response(fixture(), "test", REPORT)
        self.assertEqual((row["checkin_id"], row["extraction_method"]), ("test", "llm"))
        for payload in ("bad json", fixture(frequency_count=True), fixture(severity_score=11),
                        fixture(status="maybe"), fixture(measure=[]), fixture(diagnosis="test"),
                        '{"observations":[],"observations":[]}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_response(payload, "test", REPORT)

    def test_unsupported_evidence(self):
        with self.assertRaises(ValueError):
            validate_response(fixture(original_phrase="I vomited."), "test", REPORT)
        report = "The phone rang this afternoon. " + REPORT
        model = validate_response(
            fixture(original_phrase="The phone rang this afternoon.", frequency_count=None),
            "test", report,
        )
        result = compare(report=report, model=model)
        self.assertEqual(result["review_status"], "needs_review")
        self.assertIn("could not be corroborated", result["review_reason"])

    def test_agreement(self):
        result = compare()
        self.assertEqual(result["parser_comparison"], "agreement")
        self.assertEqual(result["reconciliation_decision"], "eligible_for_automatic_acceptance")
        self.assertEqual(result["proposed_observation"]["frequency_count"], 2)
        self.assertEqual((result["review_status"], result["validation_status"]), ("pending", "pending"))

    def test_disagreement(self):
        model = validate_response(fixture(), "test", REPORT)
        model[0]["frequency_count"] = 3
        result = compare(model=model)
        self.assertEqual(result["parser_comparison"], "disagreement")
        self.assertIsNone(result["proposed_observation"])

    def test_normalized_location_and_unsupported_optional_fields(self):
        report = "The back of my throat is sore again, around a 3 today."
        raw = fixture(measure="pain", original_phrase=report, pain_location="back of throat",
                      severity_score=3, frequency_count=1, frequency_period="daily",
                      observed_date="today", observed_time_text="today")
        with self.assertRaises(ValueError):
            validate_response(raw, "test", report)
        row, = validate_response(raw, "test", report, retain_partial=True)
        self.assertEqual(row["pain_location"], "back of throat")
        self.assertEqual(row["original_phrase"], report)
        self.assertEqual(row["observed_time_text"], "today")
        self.assertEqual(row["review_status"], "needs_review")
        self.assertEqual(set(row["rejected_fields"]),
                         {"severity_score", "frequency_count", "frequency_period", "observed_date"})
        self.assertEqual(row["rejected_fields"]["severity_score"]["value"], 3)
        self.assertTrue(all(row[field] is None for field in row["rejected_fields"]))
        result = compare(report=report, model=[row])
        self.assertEqual(result["review_status"], "needs_review")
        self.assertIsNone(result["proposed_observation"])

    def test_location_normalization_cannot_invent_anatomy(self):
        report = "My left jaw hurts."
        raw = fixture(measure="pain", original_phrase=report, pain_location="right jaw",
                      frequency_count=None, observed_time_text=None)
        row, = validate_response(raw, "test", report, retain_partial=True)
        self.assertIsNone(row["pain_location"])
        self.assertEqual(row["rejected_fields"]["pain_location"]["value"], "right jaw")

    def test_one_parser_only(self):
        self.assertEqual(compare(model=[])["parser_comparison"], "one_parser_only")
        model = validate_response(fixture(), "test", REPORT)
        row, = reconcile("test", SUBMITTED, REPORT, [], [], model)
        self.assertEqual(row["parser_comparison"], "one_parser_only")

    def test_questionnaire_conflict_requires_overlap(self):
        answer = dict(measure="vomiting", status="absent", frequency_count=0,
                      observed_time_text="today")
        self.assertEqual(compare(questionnaire=[answer])["questionnaire_relation"], "conflict")
        answer.pop("observed_time_text")
        result = compare(questionnaire=[answer])
        self.assertEqual(result["questionnaire_relation"], "temporal_ambiguity")
        self.assertEqual(result["review_status"], "needs_review")

    def test_different_time_windows(self):
        answer = dict(measure="vomiting", status="absent", frequency_count=0,
                      observed_time_text="yesterday")
        result = compare(questionnaire=[answer])
        self.assertEqual(result["questionnaire_relation"], "distinct_windows")
        self.assertEqual(result["evidence"]["questionnaire"], [answer])
        self.assertIsNotNone(result["proposed_observation"])

    def test_duplicates_and_independent_mentions(self):
        answer = dict(measure="vomiting", status="present", frequency_count=2,
                      observed_time_text="today")
        result = compare(questionnaire=[answer])
        self.assertEqual(result["questionnaire_relation"], "possible_duplicate")
        self.assertIsNone(result["proposed_observation"])
        result = compare(report=REPORT + " I threw up once this morning.")
        self.assertEqual(result["parser_comparison"], "multiple_mentions")
        self.assertEqual(len(result["evidence"]["keyword"]), 2)
        self.assertIsNone(result["proposed_observation"])

    def test_no_automatic_clinical_inference(self):
        report = "My throat hurts badly after my medication."
        raw = fixture(measure="pain", original_phrase=report, frequency_count=None,
                      observed_time_text=None, severity_score=None, pain_location="throat")
        model = validate_response(raw, "test", report)
        model[0]["severity_score"] = 9
        result = compare(report=report, model=model)
        self.assertEqual(result["review_status"], "needs_review")
        self.assertIsNone(result["proposed_observation"])

    def test_first_three_seeded_reports_require_literal_dates_and_counts(self):
        with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as db:
            reports = db.execute(
                "SELECT checkin_id, free_text FROM checkins ORDER BY checkin_id LIMIT 3"
            ).fetchall()
        for index, (key, report) in enumerate(reports, 1):
            self.assertEqual(key, f"demo-P001-{index:02d}-1")
            with self.subTest(checkin=key):
                fields = dict(measure="pain", original_phrase=report,
                              pain_location="throat" if index < 3 else "back of my throat",
                              severity_score=3 if index < 3 else None, frequency_count=None,
                              observed_time_text="at lunch" if index < 3 else "today")
                row, = validate_response(fixture(**fields), key, report)
                self.assertIsNone(row["observed_date"])
                self.assertEqual(row["original_phrase"], report)
                unsupported = [("observed_date", "2023-11-09"), ("frequency_count", 1)]
                if index == 3:
                    unsupported.extend([("frequency_period", "daily"), ("severity_score", 3)])
                else:
                    unsupported.append(("observed_time_text", "about 3 out of 10"))
                for field, value in unsupported:
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        validate_response(fixture(**(fields | {field: value})), key, report)

    @patch("src.llm_extraction.request_json", return_value={"models": []})
    def test_unavailable_model_stops_before_inference(self, request):
        result = extract_live([("test", REPORT)], DEFAULT_MODEL)
        request.assert_called_once_with("/api/tags")
        self.assertEqual(result["execution_mode"], "blocked")
        self.assertIsNone(result["model"])
        self.assertEqual(result["processed_checkin_ids"], [])
        self.assertEqual(compare(model=[], available=False)["parser_comparison"], "llm_unavailable")

    @patch("src.llm_extraction.request_json", side_effect=PermissionError("blocked"))
    def test_unavailable_service(self, request):
        result = extract_live([("test", REPORT)], DEFAULT_MODEL)
        request.assert_called_once_with("/api/tags")
        self.assertEqual(result["candidates"], [])
        self.assertIn("blocked", result["blocker"])
