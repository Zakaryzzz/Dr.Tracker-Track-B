from src.identifiers import identifier
import json
import unittest
from contextlib import closing

from src.database import DATABASE_PATH, connect
from src.extract_symptoms import map_questionnaire, parse_keywords


class ExtractionTests(unittest.TestCase):
    def by_measure(self, text):
        return {row["measure"]: row for row in parse_keywords("test", text)}

    def test_negation_stays_local(self):
        rows = self.by_measure("I haven't vomited today. My mouth feels dry.")
        self.assertEqual(rows["vomiting"]["status"], "absent")
        self.assertIsNone(rows["vomiting"]["frequency_count"])
        self.assertEqual(rows["dry_mouth"]["status"], "present")

    def test_negated_list(self):
        rows = self.by_measure("No fever or chills but I feel exhausted.")
        self.assertEqual(rows["fever"]["status"], "absent")
        self.assertEqual(rows["chills"]["status"], "absent")
        self.assertEqual(rows["fatigue"]["status"], "present")

    def test_uncertainty_and_time(self):
        row = self.by_measure("I think I felt dizzy by the sink this morning.")["dizziness"]
        self.assertEqual(row["status"], "uncertain")
        self.assertEqual(row["observed_time_text"], "this morning")

    def test_explicit_counts(self):
        for phrase, count in (("once", 1), ("twice", 2), ("three times", 3), ("4 times", 4)):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.by_measure(f"I threw up {phrase}.")["vomiting"]["frequency_count"], count)

    def test_multiple_symptoms_and_explicit_severity(self):
        rows = self.by_measure("My throat hurts about 7 out of 10 and I feel dizzy.")
        self.assertEqual(set(rows), {"pain", "dizziness"})
        self.assertEqual(rows["pain"]["severity_score"], 7)
        self.assertIsNone(rows["dizziness"]["severity_score"])

    def test_missing_and_explicit_questionnaire_answers(self):
        answers = dict(pain_score=0, pain_location="left side, near an old scar",
                       fever=False, bleeding=True, fatigue_score=None,
                       dizziness_score="uncertain", vomiting_count=3)
        rows = {row["measure"]: row for row in map_questionnaire("test", json.dumps(answers))}
        self.assertNotIn("fatigue", rows)
        self.assertEqual(rows["pain"]["status"], "absent")
        self.assertEqual(rows["pain"]["severity_score"], 0)
        self.assertEqual(rows["pain"]["pain_location"], answers["pain_location"])
        self.assertEqual(rows["fever"]["status"], "absent")
        self.assertEqual(rows["bleeding"]["status"], "present")
        self.assertEqual(rows["dizziness"]["status"], "uncertain")
        self.assertEqual(rows["vomiting"]["frequency_count"], 3)

    def test_vague_severity_and_pain_location(self):
        text = "My throat hurts badly."
        row = self.by_measure(text)["pain"]
        self.assertIsNone(row["severity_score"])
        self.assertEqual(row["pain_location"], "throat")
        self.assertEqual(row["original_phrase"], text)
        self.assertEqual(self.by_measure("The pain is under my jaw.")["pain"]["pain_location"],
                         "is under my jaw")

    def test_medication_mention_is_not_pain(self):
        self.assertEqual(parse_keywords("test", "My team adjusted my pain medication."), [])

    def test_seeded_checkins(self):
        examples = (
            (identifier("checkins", "demo-P002-12-1"), "vomiting", "absent", None),
            (identifier("checkins", "demo-P005-06-1"), "dizziness", "uncertain", None),
            (identifier("checkins", "demo-P005-14-1"), "vomiting", "present", 2),
        )
        with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as connection:
            for checkin_id, measure, status, count in examples:
                with self.subTest(checkin_id=checkin_id):
                    raw, text = connection.execute(
                        "SELECT raw_answers, free_text FROM checkins WHERE checkin_id = ?",
                        (checkin_id,),
                    ).fetchone()
                    keyword = self.by_measure(text)[measure]
                    self.assertEqual((keyword["status"], keyword["frequency_count"]), (status, count))
                    if checkin_id == identifier("checkins", "demo-P005-14-1"):
                        mapped = next(row for row in map_questionnaire(checkin_id, raw)
                                      if row["measure"] == "vomiting")
                        self.assertEqual((mapped["status"], mapped["frequency_count"]), ("absent", 0))
                        self.assertEqual(mapped["source_type"], "questionnaire")
                        self.assertEqual(keyword["source_type"], "free_text")
