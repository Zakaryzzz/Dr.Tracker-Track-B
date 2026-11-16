import unittest
from contextlib import closing

from src.analysis import open_readonly
from src.evaluate import DATA, DEFAULT_REFERENCE, evaluate_parser, evaluate_reconciliation, integrity, load, validate_reference, verified


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.report = "I think I felt dizzy today."
        self.key = "c1"
        self.label = dict(checkin_id=self.key, measure="dizziness", status="uncertain",
                          original_phrase=self.report, observed_time_text="today",
                          frequency_count=None, severity_score=None)
        self.case = dict(checkin_id=self.key, original_free_text=self.report,
                         submitted_at="2026-01-01T08:00:00", mentions=[self.label])

    def test_detection_status_and_one_to_one_matching(self):
        predictions = [dict(self.label, original_phrase="felt dizzy today.", status="present")] * 2
        result = evaluate_parser([self.case], predictions)
        self.assertEqual(result["detection"],
                         dict(tp=1, fp=1, fn=0, precision=0.5, recall=1, f1=2/3))
        self.assertEqual(result["detection_plus_status"]["tp"], 0)
        self.assertEqual(result["status_on_detection_matches"]["accuracy"], 0)
        self.assertEqual(result["fields_on_detection_matches"]["uncertainty"]["supported"], 1)

    def test_failed_checkin_keeps_reference_denominator(self):
        result = evaluate_parser([self.case], [self.label], failures=[self.key])
        self.assertEqual(result["detection"]["fn"], 1)
        self.assertEqual(result["detection"]["recall"], 0)
        self.assertIsNone(result["detection"]["precision"])
        self.assertEqual(result["reference_mentions_in_failed_checkins"], 1)

    def test_partial_candidate_does_not_get_credit_for_rejected_field(self):
        prediction = dict(self.label, rejected_fields={"severity_score": {"value": 9}})
        result = evaluate_parser([self.case], [prediction], partial=[self.key])
        self.assertEqual(result["detection"]["tp"], 1)
        self.assertEqual(result["fields_on_detection_matches"]["severity"]["correct"], 0)
        self.assertEqual(result["partial_checkin_ids"], [self.key])

    def test_draft_requires_human_verification_and_literal_evidence(self):
        self.assertFalse(verified({"verification_status": "verified", "verified_by": " "}))
        self.assertTrue(verified({"verification_status": "verified", "verified_by": "Reviewer"}))
        pending = evaluate_reconciliation([self.case], [], {"duplicate_pairs": []}, False)
        self.assertEqual(pending["agreement_correctness"]["status"], "pending_human_verification")
        bad_case = dict(self.case, mentions=[dict(self.label, original_phrase="I felt dizzy.")])
        with self.assertRaisesRegex(ValueError, "Bad reference"):
            validate_reference({"cases": [bad_case]}, {self.key: (self.report, self.case["submitted_at"])})

    def test_verified_duplicate_pair_and_agreement_correctness(self):
        second = dict(self.case, checkin_id="c2", mentions=[])
        reference = {"duplicate_pairs": [
            dict(checkin_ids=["c1", "c2"], measure="dizziness", same_event=True,
                 verification_status="verified", verified_by="Fixture reviewer"),
        ]}
        group = dict(checkin_id=self.key, measure="dizziness", text_parser_status="agreement",
                     agreed_text_observation=self.label, possible_duplicate_links=["c2"])
        result = evaluate_reconciliation([self.case, second], [group], reference, True)
        self.assertEqual(result["duplicate_pair_detection"]["tp"], 1)
        self.assertEqual(result["agreement_correctness"]["exact_scored_claims"], 1)

    def test_saved_sample_and_readonly_integrity(self):
        reference = load(DEFAULT_REFERENCE)
        with closing(open_readonly()) as db:
            reports = {key: (text, timestamp) for key, text, timestamp in db.execute(
                "SELECT checkin_id, free_text, submitted_at FROM checkins"
            )}
            validate_reference(reference, reports)
            checks = integrity(db, load(DATA / "phase3c1_reconciliation.json"))
        self.assertEqual(len(reference["cases"]), 36)
        self.assertEqual(checks["status"], "passed")
        self.assertTrue(checks["dashboard_read_only"])
        self.assertEqual(checks["stored_observations"], 1460)
