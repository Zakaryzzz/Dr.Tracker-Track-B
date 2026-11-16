import json
import unittest
from contextlib import closing

from src.database import DATABASE_PATH, connect
from src.extract_symptoms import map_questionnaire, parse_keywords
from src.reconcile_phase3c import (
    compare_group, link_checkins, observation, validate_attempt,
)

KEY = "test"
REPORT = "I threw up twice this afternoon, even though I marked zero earlier."


def vomiting(**overrides):
    item = dict(measure="vomiting", status="present", severity_score=None,
                frequency_count=2, frequency_period="this afternoon",
                pain_location=None, observed_date=None,
                observed_time_text="this afternoon", original_phrase=REPORT)
    item.update(overrides)
    return item


class Phase3CReviewTests(unittest.TestCase):
    def test_questionnaire_only_and_stable_id(self):
        answers = map_questionnaire(KEY, '{"pain_score":0}')
        group = compare_group(KEY, "pain", "", answers, [], [], [], False, [])
        row, = group["questionnaire_observations"]
        self.assertEqual(group["review_reasons"], [])
        self.assertEqual(row["validation_status"], "passed")
        self.assertIsNone(row["observed_date"])
        self.assertIsNone(row["observed_time_text"])
        self.assertEqual(row["observation_id"], observation(answers[0], "questionnaire")["observation_id"])
        self.assertNotEqual(row["observation_id"], observation(answers[0], "free_text")["observation_id"])

    def test_contained_quotes_agree_despite_unknown_questionnaire_timing(self):
        rules = parse_keywords(KEY, REPORT)
        model, rejected, _ = validate_attempt(json.dumps({"observations": [vomiting()]}),
                                              KEY, REPORT, rules)
        answers = map_questionnaire(KEY, '{"vomiting_count":0}')
        group = compare_group(KEY, "vomiting", REPORT, answers, rules, model, rules, True, rejected)
        self.assertEqual(group["text_parser_status"], "agreement")
        self.assertEqual(group["questionnaire_text_relationship"], "time_unknown")
        self.assertEqual(group["review_reasons"], ["questionnaire_time_unknown"])
        self.assertEqual(group["evidence"]["llm"][0]["original_phrase"], REPORT)
        self.assertEqual(group["agreed_text_observation"]["frequency_count"], 2)

    def test_partial_response_with_invalid_sibling(self):
        rules = parse_keywords(KEY, REPORT)
        raw = json.dumps({"observations": [vomiting(), vomiting(original_phrase="I vomited.") ]})
        valid, rejected, parsed = validate_attempt(raw, KEY, REPORT, rules)
        self.assertTrue(parsed)
        self.assertEqual(len(valid), 1)
        self.assertEqual(rejected[0]["index"], 1)

    def test_unscaled_severity_remains_rejected(self):
        report = "The back of my throat is sore again, around a 3 today."
        rule, = parse_keywords(KEY, report)
        item = vomiting(measure="pain", severity_score=3, frequency_count=None,
                        frequency_period=None, pain_location="back of throat",
                        observed_time_text="today", original_phrase=report)
        valid, rejected, _ = validate_attempt(json.dumps({"observations": [item]}),
                                               KEY, report, [rule])
        self.assertEqual(rejected, [])
        self.assertIsNone(valid[0]["severity_score"])
        self.assertEqual(valid[0]["rejected_fields"]["severity_score"]["value"], 3)

    def test_seeded_duplicate_and_distinct_occurrences(self):
        ids = ("demo-P005-06-1", "demo-P005-06-2", "demo-P005-17-1", "demo-P005-17-2")
        with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as db:
            checkins = {key: db.execute(
                "SELECT patient_id, submitted_at, free_text, raw_answers FROM checkins WHERE checkin_id=?",
                (key,),
            ).fetchone() for key in ids}
        groups = {}
        for key, (_, _, report, _) in checkins.items():
            rules = parse_keywords(key, report)
            groups[key] = compare_group(key, "dizziness", report, [], rules, [], rules, False, [])
        link_checkins(groups.values(), checkins)
        self.assertEqual(groups[ids[0]]["possible_duplicate_links"], [ids[1]])
        self.assertEqual(groups[ids[1]]["possible_duplicate_links"], [ids[0]])
        self.assertEqual(groups[ids[3]]["possible_duplicate_links"], [])
        self.assertEqual(groups[ids[3]]["distinct_occurrence_links"][0]["checkin_id"], ids[2])
        self.assertNotEqual(groups[ids[2]]["supported_text_candidates"][0]["observation_id"],
                            groups[ids[3]]["supported_text_candidates"][0]["observation_id"])
