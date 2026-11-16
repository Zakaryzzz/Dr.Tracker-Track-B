"""Evaluate saved extraction outputs only after independent human verification."""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from .analysis import RAW, VALIDATED, daily_weight, open_readonly, pain_trend, treatment_windows, weekly_vomiting
from .database import DATABASE_PATH, connect
from .extract_symptoms import PATTERNS
from .insert_symptoms import process
from .llm_extraction import STATUSES
from .reconcile_phase3c import source_span
from .reconcile_symptoms import measure_of, normalize, write_json

DATA = DATABASE_PATH.parent
DEFAULT_REFERENCE = DATA / "phase5_reference_draft.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verified(item):
    reviewer = item.get("verified_by")
    return (item.get("verification_status") == "verified"
            and isinstance(reviewer, str) and bool(reviewer.strip()))


def validate_reference(reference, reports):
    cases = reference["cases"]
    ids = [case["checkin_id"] for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("Reference must contain unique check-ins")
    for case in cases:
        key = case["checkin_id"]
        if reports.get(key) != (case["original_free_text"], case["submitted_at"]):
            raise ValueError(f"Reference source differs from SQLite for {key}")
        seen = set()
        for row in case["mentions"]:
            span = source_span(case["original_free_text"], row["original_phrase"])
            if (span is None or row["checkin_id"] != key
                    or row["measure"] not in PATTERNS or row["status"] not in STATUSES):
                raise ValueError(f"Bad reference evidence or label for {key}")
            mention_key = row["measure"], span
            if mention_key in seen:
                raise ValueError(f"Repeated reference mention for {key}: {mention_key}")
            seen.add(mention_key)
            for name, upper in (("severity_score", 10), ("frequency_count", None)):
                value = row[name]
                if value is not None and (type(value) is not int or value < 0
                                          or (upper is not None and value > upper)):
                    raise ValueError(f"Bad reference {name} for {key}")


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def metrics(tp, predicted, actual):
    return dict(tp=tp, fp=predicted - tp, fn=actual - tp,
                precision=ratio(tp, predicted), recall=ratio(tp, actual),
                f1=ratio(2 * tp, predicted + actual))


def mention_pairs(report, predictions, references, require_status=False):
    """Match same-measure overlapping literal spans one to one."""
    expected_spans = [source_span(report, row.get("original_phrase")) for row in references]
    adjacency = []
    for row in predictions:
        span = source_span(report, row.get("original_phrase"))
        options = []
        for index, (reference, expected) in enumerate(zip(references, expected_spans)):
            if not span or not expected or measure_of(row) != measure_of(reference):
                continue
            if require_status and row["status"] != reference["status"]:
                continue
            overlap = min(span[1], expected[1]) - max(span[0], expected[0])
            if overlap > 0:
                options.append((span != expected, -overlap, index))
        adjacency.append([index for _, _, index in sorted(options)])

    owners = {}

    def assign(prediction, seen):
        for index in adjacency[prediction]:
            if index in seen:
                continue
            seen.add(index)
            if index not in owners or assign(owners[index], seen):
                owners[index] = prediction
                return True
        return False

    for prediction in sorted(range(len(predictions)), key=lambda index: len(adjacency[index])):
        assign(prediction, set())
    return [(predictions[prediction], references[index]) for index, prediction in sorted(owners.items())]


def field_values(row):
    return {
        "severity": row.get("severity_score"),
        "frequency": (row.get("frequency_count"), normalize(row.get("frequency_period"))),
        "frequency_count": row.get("frequency_count"),
        "frequency_period": normalize(row.get("frequency_period")),
        "negation": row["status"] == "absent",
        "uncertainty": row["status"] == "uncertain",
        "temporal_evidence": (row.get("observed_date"), normalize(row.get("observed_time_text"))),
    }


def evaluate_parser(cases, predictions, failures=(), partial=()):
    by_key = defaultdict(list)
    for row in predictions:
        by_key[row["checkin_id"]].append(row)
    keys = {case["checkin_id"] for case in cases}
    actual = predicted = detections = statuses = 0
    fields = defaultdict(Counter)
    confusion = Counter()
    failures = keys.intersection(failures)
    failed_reference_mentions = 0
    for case in cases:
        key = case["checkin_id"]
        refs = case["mentions"]
        # Failed check-ins contribute their reference mentions to the
        # denominator with no retained predictions.
        candidates = [] if key in failures else by_key[key]
        if key in failures:
            failed_reference_mentions += len(refs)
        actual += len(refs)
        predicted += len(candidates)
        pairs = mention_pairs(case["original_free_text"], candidates, refs)
        detections += len(pairs)
        statuses += len(mention_pairs(case["original_free_text"], candidates, refs, require_status=True))
        for prediction, reference in pairs:
            confusion[(reference["status"], prediction["status"])] += 1
            observed = field_values(prediction)
            rejected = prediction.get("rejected_fields", {})
            field_names = {
                "severity": {"severity_score"}, "frequency": {"frequency_count", "frequency_period"},
                "frequency_count": {"frequency_count"}, "frequency_period": {"frequency_period"},
                "temporal_evidence": {"observed_date", "observed_time_text"},
            }
            for field, expected in field_values(reference).items():
                correct = observed[field] == expected and not field_names.get(field, set()).intersection(rejected)
                tally = fields[field]
                tally["matched"] += 1
                tally["correct"] += correct
                supported = any(value is not None for value in expected) if isinstance(expected, tuple) else (
                    expected if isinstance(expected, bool) else expected is not None
                )
                if supported:
                    tally["supported"] += 1
                    tally["supported_correct"] += correct
    return {
        "detection": metrics(detections, predicted, actual),
        "detection_plus_status": metrics(statuses, predicted, actual),
        "status_on_detection_matches": {
            "accuracy": ratio(sum(count for (a, b), count in confusion.items() if a == b), detections),
            "confusion": [{"reference": a, "prediction": b, "count": count}
                          for (a, b), count in sorted(confusion.items())],
        },
        "fields_on_detection_matches": {
            field: {"correct": fields[field]["correct"], "matched": fields[field]["matched"],
                    "accuracy": ratio(fields[field]["correct"], fields[field]["matched"]),
                    "supported_correct": fields[field]["supported_correct"],
                    "supported": fields[field]["supported"],
                    "accuracy_on_supported_reference": ratio(fields[field]["supported_correct"],
                                                             fields[field]["supported"])}
            for field in field_values({"status": "present"})
        },
        "failed_checkin_ids": sorted(failures),
        "reference_mentions_in_failed_checkins": failed_reference_mentions,
        "partial_checkin_ids": sorted(keys.intersection(partial)),
    }


def operational(groups, audit):
    text = [group for group in groups if group["text_parser_status"] != "no_text_candidate"]
    reviewed = sum(group["review_status"] == "reviewed" for group in groups)
    agreements = sum(group["text_parser_status"] == "agreement" for group in text)
    review = sum(group["review_status"] == "needs_review" for group in groups)
    return {
        "groups": len(groups), "text_groups": len(text), "agreements": agreements,
        "agreement_rate_among_text_groups": ratio(agreements, len(text)),
        "groups_requiring_review": review, "review_rate_all_groups": ratio(review, len(groups)),
        "review_rate_text_groups": ratio(sum(g["review_status"] == "needs_review" for g in text), len(text)),
        "review_reasons_overlapping": dict(Counter(reason for group in groups for reason in group["review_reasons"])),
        "groups_with_possible_duplicate_links": sum(bool(g["possible_duplicate_links"]) for g in groups),
        "groups_with_distinct_occurrence_links": sum(bool(g["distinct_occurrence_links"]) for g in groups),
        "rejected_observations_across_saved_attempts": sum(
            len(attempt["rejected"]) for item in audit.values() for attempt in item["attempts"].values()
        ),
        "human_reviewed_groups": reviewed,
        "confirmed_clinical_events": None,
    }


def evaluate_reconciliation(cases, groups, reference, verified_cases):
    agreements = [g["agreed_text_observation"] for g in groups if g["text_parser_status"] == "agreement"]
    by_case = defaultdict(list)
    for row in agreements:
        by_case[row["checkin_id"]].append(row)
    correct_mentions = correct_claims = 0
    if verified_cases:
        for case in cases:
            for row, label in mention_pairs(case["original_free_text"], by_case[case["checkin_id"]],
                                            case["mentions"], require_status=True):
                correct_mentions += 1
                correct_claims += field_values(row) == field_values(label)
    pairs = reference.get("duplicate_pairs", [])
    duplicate_result = {"status": "pending_human_verification"}
    if verified_cases and pairs and all(verified(pair) for pair in pairs):
        index = {(g["checkin_id"], g["measure"]): g for g in groups}
        actual = predicted = tp = 0
        for pair in pairs:
            a, b = pair["checkin_ids"]
            label = pair["same_event"]
            if not isinstance(label, bool) or a == b or any(key not in {c["checkin_id"] for c in cases} for key in (a, b)):
                raise ValueError("Invalid duplicate reference pair")
            left = index.get((a, pair["measure"]), {})
            right = index.get((b, pair["measure"]), {})
            flagged = b in left.get("possible_duplicate_links", []) or a in right.get("possible_duplicate_links", [])
            actual += label
            predicted += flagged
            tp += label and flagged
        duplicate_result = {"status": "computed", "labeled_pairs": len(pairs), **metrics(tp, predicted, actual)}
    return {
        "agreement_correctness": (
            {"status": "computed", "agreements": len(agreements),
             "correct_measure_and_status": correct_mentions, "exact_scored_claims": correct_claims,
             "measure_status_accuracy": ratio(correct_mentions, len(agreements)),
             "exact_scored_claim_accuracy": ratio(correct_claims, len(agreements))}
            if verified_cases else {"status": "pending_human_verification"}
        ),
        "duplicate_pair_detection": duplicate_result,
    }


def integrity(db, reconciliation):
    # Import verification compares SQLite tuples. Dashboard queries use Row.
    with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as import_db:
        batch = process(import_db, reconciliation, "verify", expected_count=1460)
    with db:
        counts = tuple(db.execute("""
            SELECT COUNT(*), COUNT(DISTINCT observation_id),
                   SUM(source_type <> 'questionnaire'),
                   SUM(observed_date IS NOT NULL OR observed_time_text IS NOT NULL),
                   SUM(severity_score = 0), SUM(frequency_count = 0),
                   SUM(status = 'uncertain'), SUM(severity_score IS NULL)
            FROM symptoms
        """).fetchone())
        repeated = db.execute("""
            SELECT 1 FROM symptoms GROUP BY checkin_id, measure, source_type HAVING COUNT(*) > 1
        """).fetchone()
        if counts[:4] != (1460, 1460, 0, 0) or repeated is not None:
            raise ValueError("Unexpected stored symptom inventory")
        symptom_keys = {tuple(row) for row in db.execute("SELECT checkin_id, measure FROM symptoms")}
        if any((g["checkin_id"], g["measure"]) in symptom_keys for g in reconciliation["results"]
               if g["review_status"] == "needs_review"):
            raise ValueError("A held group was inserted")
        raw_pain, validated_pain = [
            pain_trend(db, "P001", layer) for layer in (RAW, VALIDATED)
        ]
        totals = Counter()
        for patient, in db.execute("SELECT patient_id FROM patients"):
            for row in weekly_vomiting(db, patient):
                totals.update({key: row[key] for key in ("positive", "explicit_zero", "uncertain", "missing", "submissions")})
        adjusted = next(row for row in treatment_windows(db, "P003")
                        if row["measure"] == "pain" and row["event_status"] == "adjusted")
        weights = daily_weight(db, "P005")
        if (len(raw_pain) != 29 or len(validated_pain) != 29
                or any(not row["checkin_id"].startswith("demo-P001-") for row in raw_pain + validated_pain)
                or sum(row["pain_score"] is not None for row in raw_pain) != 29
                or any(row["pain_score"] is not None for row in validated_pain)
                or dict(totals) != dict(positive=7, explicit_zero=128, uncertain=0, missing=8, submissions=143)
                or tuple(adjusted[key] for key in ("before_submissions", "after_submissions", "before_scores", "after_scores"))
                   != (7, 7, 3, 7)
                or len(weights) != 12 or sum(row["measurements_that_day"] for row in weights) != 14):
            raise ValueError("Dashboard data contract differs from the Phase 4 baseline")
    # Try a zero-row write so even an incorrectly writable connection cannot change data.
    try:
        db.execute("UPDATE patients SET patient_name = patient_name WHERE 0")
    except sqlite3.OperationalError as error:
        if error.sqlite_errorname != "SQLITE_READONLY":
            raise
    else:
        db.rollback()
        raise ValueError("Dashboard connection unexpectedly permits writes")
    return {
        "status": "passed", "stored_observations": batch["symptoms_total"],
        "foreign_key_check": batch["foreign_key_check"], "held_groups_absent_from_sqlite": True,
        "explicit_zero_scores": counts[4], "explicit_zero_counts": counts[5],
        "stored_uncertain_observations": counts[6], "null_severity_scores": counts[7],
        "dashboard_read_only": True, "patient_filter_and_layer_separation": "passed",
        "weekly_raw_vomiting": dict(totals), "adjustment_window_coverage": [7, 7, 3, 7],
        "p005_weights": {"measurements": 14, "displayed_days": 12},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output", type=Path, default=DATA / "phase5_evaluation.json")
    args = parser.parse_args()
    output = args.output.resolve()
    protected = set(DATA.resolve().iterdir()) - {DATA.resolve() / "phase5_evaluation.json"}
    if output in protected or output == args.reference.resolve() or output.suffix != ".json":
        parser.error("Choose a separate JSON output path")
    reference = load(args.reference)
    keyword = load(DATA / "keyword_extractions.json")
    llm = load(DATA / "llm_extractions.json")
    reconciliation = load(DATA / "phase3c1_reconciliation.json")
    cases = reference["cases"]
    ids = {case["checkin_id"] for case in cases}
    with closing(open_readonly()) as db:
        reports = {key: (text, timestamp) for key, text, timestamp in db.execute(
            "SELECT checkin_id, free_text, submitted_at FROM checkins"
        )}
        validate_reference(reference, reports)
        checks = integrity(db, reconciliation)

    approved = all(verified(case) for case in cases)
    groups = reconciliation["results"]
    audit = reconciliation["llm_revalidation_audit"]
    sample_groups = [group for group in groups if group["checkin_id"] in ids]
    sample_audit = {key: value for key, value in audit.items() if key in ids}
    failed = sorted(ids.intersection(llm["errors"]))
    partial = sorted({row["checkin_id"] for row in llm["candidates"]
                      if row.get("rejected_fields") and row["checkin_id"] in ids})
    result = {
        "sample": {"checkins": len(cases), "distinct_reports": len({case["original_free_text"] for case in cases}),
                   "reference_mentions": sum(len(case["mentions"]) for case in cases),
                   "verified_checkins": sum(verified(case) for case in cases)},
        "reference_status": "verified" if approved else "pending_human_verification",
        "saved_llm": {"model": llm["model"], "sample_failed_checkin_ids": failed,
                      "sample_partial_checkin_ids": partial},
        "saved_candidate_counts_in_sample": {
            "keyword": sum(row["checkin_id"] in ids for row in keyword),
            "llm": sum(row["checkin_id"] in ids for row in llm["candidates"]),
        },
        "parser_accuracy": (
            {"keyword": evaluate_parser(cases, keyword),
             "llm": evaluate_parser(cases, llm["candidates"], failed, partial)}
            if approved else {"status": "pending_human_verification"}
        ),
        "reconciliation": {
            "full_run": operational(groups, audit),
            "sample": operational(sample_groups, sample_audit),
            **evaluate_reconciliation(cases, sample_groups, reference, approved),
        },
        "integrity": checks,
    }
    write_json(output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
