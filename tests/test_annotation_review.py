import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from src.annotation_review import edit_case, edit_pair, export_reference, load_review, save_item
from src.evaluate import DEFAULT_REFERENCE, load, verified


class AnnotationReviewTests(unittest.TestCase):
    def setUp(self):
        self.review = load(DEFAULT_REFERENCE)
        self.case = self.review["cases"][0]
        self.pair = self.review["duplicate_pairs"][0]
        self.reports = {case["checkin_id"]: (case["original_free_text"], case["submitted_at"])
                        for case in self.review["cases"]}

    def test_verification_requires_explicit_confirmation_and_identifier(self):
        draft = edit_case(self.case, self.case["mentions"], False, "Reviewer")
        self.assertFalse(verified(draft))
        with self.assertRaisesRegex(ValueError, "reviewer identifier"):
            edit_case(self.case, self.case["mentions"], True, " ")
        verified_case = edit_case(self.case, self.case["mentions"], True, " Reviewer ")
        self.assertTrue(verified(verified_case))
        self.assertEqual(verified_case["verified_by"], "Reviewer")
        self.assertEqual(verified_case["original_free_text"], self.case["original_free_text"])
        self.assertFalse(verified(self.case))

    def test_add_remove_and_correct_mentions(self):
        row = {**self.case["mentions"][0], "severity_score": None}
        empty = edit_case(self.case, [], False, "")
        self.assertEqual(empty["mentions"], [])
        updated = edit_case(empty, [row], False, "")
        self.assertIsNone(updated["mentions"][0]["severity_score"])
        self.assertEqual(updated["mentions"][0]["checkin_id"], self.case["checkin_id"])

    def test_reject_nonliteral_evidence_and_unsupported_window(self):
        for updates, reason in (
            ({"original_phrase": "It hurt at lunch"}, "Bad reference"),
            ({"observed_time_text": "yesterday"}, "literal wording"),
            ({"frequency_period": "at lunch"}, "requires a reported count"),
            ({"severity_score": 3.5}, "Bad reference severity_score"),
        ):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, reason):
                edit_case(self.case, [{**self.case["mentions"][0], **updates}], True, "Reviewer")

    def test_save_resumes_without_overwriting_draft_or_earlier_review(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            draft = Path(directory) / "draft.json"
            progress = Path(directory) / "reviewed.json"
            draft.write_text(json.dumps(self.review), encoding="utf-8")
            original = draft.read_bytes()
            self.assertFalse(progress.exists())
            case = edit_case(self.case, self.case["mentions"], True, "Reviewer")
            save_item("cases", 0, self.case, case, draft_path=draft, progress_path=progress)
            pair = edit_pair(self.pair, False, "Fixture decision", True, "Reviewer")
            save_item("duplicate_pairs", 0, self.pair, pair, draft_path=draft, progress_path=progress)
            resumed = load_review(draft, progress)
            self.assertEqual(resumed["cases"][0], case)
            self.assertEqual(resumed["duplicate_pairs"][0], pair)
            self.assertEqual(draft.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "changed in another session"):
                save_item("cases", 0, self.case, self.case, draft_path=draft, progress_path=progress)

    def test_export_gate_and_evaluator_compatible_payload(self):
        with self.assertRaisesRegex(ValueError, "Complete all"):
            export_reference(self.review, self.reports)
        reviewed = deepcopy(self.review)
        for case in reviewed["cases"]:
            case.update(verification_status="verified", verified_by="Test fixture")
        payload = json.loads(export_reference(reviewed, self.reports))
        self.assertTrue(all(verified(case) for case in payload["cases"]))
        self.assertFalse(any(verified(pair) for pair in payload["duplicate_pairs"]))
        self.assertEqual(payload["cases"][0]["original_free_text"], self.case["original_free_text"])

    def test_pair_must_have_an_explicit_label_for_verification(self):
        self.assertFalse(verified(edit_pair(self.pair, None, None, False, "")))
        with self.assertRaisesRegex(ValueError, "Choose whether"):
            edit_pair(self.pair, None, None, True, "Reviewer")
