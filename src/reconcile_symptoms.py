"""Compare independent extraction candidates without writing to SQLite."""
import argparse
import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .database import DATABASE_PATH, connect
from .identifiers import load_artifact
from .extract_symptoms import parse_keywords
from .llm_extraction import DEFAULT_MODEL, extract_live, location_words

CLAIM_FIELDS = ("status", "severity_score", "frequency_count", "frequency_period", "pain_location")
SLOTS = {
    "this morning": "morning", "before breakfast": "morning",
    "this afternoon": "afternoon", "this evening": "evening", "tonight": "evening",
}


def normalize(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else value


def measure_of(row):
    return normalize(row["measure"]).replace("-", "_").replace(" ", "_")


def time_window(row, submitted_at):
    """Return a known local day and optional day part, or an unknown window."""
    wording = normalize(row.get("observed_time_text"))
    explicit_date = row.get("observed_date")
    submitted_day = date.fromisoformat(submitted_at[:10])
    if explicit_date:
        return date.fromisoformat(explicit_date), SLOTS.get(wording)
    if wording == "yesterday":
        return submitted_day - timedelta(days=1), None
    if wording == "today" or wording in SLOTS:
        return submitted_day, SLOTS.get(wording)
    return None


def temporal_relation(first, second, submitted_at):
    left, right = (time_window(row, submitted_at) for row in (first, second))
    if left is None or right is None:
        return "unknown"
    if left[0] != right[0] or (left[1] and right[1] and left[1] != right[1]):
        return "distinct"
    return "overlap"


def claim(row):
    return tuple(
        location_words(row[field]) if field == "pain_location" and row.get(field)
        else normalize(row.get(field))
        for field in CLAIM_FIELDS
    )


def grounded(row, report, supported):
    """Conservatively corroborate the claim against the original clause in context."""
    phrase = row.get("original_phrase")
    if not phrase or phrase not in report:
        return False
    return any(
        measure_of(row) == measure_of(reference)
        and claim(row) == claim(reference)
        and normalize(row.get("observed_time_text")) == normalize(reference.get("observed_time_text"))
        and row.get("observed_date") == reference.get("observed_date")
        and (phrase in reference["original_phrase"] or reference["original_phrase"] in phrase)
        for reference in supported
    )


def reconcile(checkin_id, submitted_at, report, questionnaire, keyword, llm,
              *, llm_available=True):
    """Group evidence by measure and preserve every mention within each group."""
    groups = defaultdict(lambda: {"questionnaire": [], "keyword": [], "llm": []})
    for source, candidates in (("questionnaire", questionnaire), ("keyword", keyword), ("llm", llm)):
        for row in candidates:
            groups[measure_of(row)][source].append(row)

    supported = parse_keywords(checkin_id, report)
    results = []
    for measure, evidence in sorted(groups.items()):
        answers, rules, model = (evidence[source] for source in ("questionnaire", "keyword", "llm"))
        text = rules + model
        reasons = []
        proposed = None
        comparison = "questionnaire_only"
        if text:
            if any(row.get("rejected_fields") for row in model):
                reasons.append("The LLM candidate contains rejected optional fields.")
            if any(not grounded(row, report, supported) for row in text):
                reasons.append("A text claim could not be corroborated in its source context.")
            if not llm_available:
                comparison = "llm_unavailable"
                reasons.append("No valid LLM comparison is available for this check-in.")
            elif not rules or not model:
                comparison = "one_parser_only"
                reasons.append("Only one text parser detected this measure.")
            elif len(rules) != 1 or len(model) != 1:
                comparison = "multiple_mentions"
                reasons.append("Multiple mentions may represent distinct events or duplicates.")
            else:
                left, right = rules[0], model[0]
                relation = temporal_relation(left, right, submitted_at)
                # Different day parts are separate events. A broad or unknown
                # window only matches when both point to the same source phrase/time.
                same_reference = (
                    normalize(left.get("original_phrase")) == normalize(right.get("original_phrase"))
                    and normalize(left.get("observed_time_text")) == normalize(right.get("observed_time_text"))
                )
                if relation == "distinct":
                    comparison = "distinct_windows"
                    reasons.append("Text candidates describe different time windows.")
                elif not same_reference:
                    comparison = "temporal_ambiguity"
                    reasons.append("A shared event cannot be established from the cited text.")
                elif claim(left) != claim(right):
                    comparison = "disagreement"
                    reasons.append("Text parsers disagree on status or structured values.")
                elif reasons:
                    comparison = "unsupported_evidence"
                else:
                    comparison = "agreement"
                    proposed = {
                        "checkin_id": checkin_id, "measure": measure,
                        **{field: left.get(field) for field in CLAIM_FIELDS},
                        "observed_date": left.get("observed_date"),
                        "observed_time_text": left.get("observed_time_text"),
                        "original_phrase": left["original_phrase"],
                        "source_type": "free_text", "extraction_method": "reconciled",
                    }

        questionnaire_relation = "not_applicable"
        if answers and text:
            relations = {temporal_relation(answer, item, submitted_at)
                         for answer in answers for item in text}
            questionnaire_relation = "distinct_windows"
            if "overlap" in relations:
                differences = any(
                    temporal_relation(answer, item, submitted_at) == "overlap"
                    and (answer["status"] != item["status"] or any(
                        answer.get(field) is not None and item.get(field) is not None
                        and answer[field] != item[field]
                        for field in ("severity_score", "frequency_count")
                    ))
                    for answer in answers for item in text
                )
                questionnaire_relation = "conflict" if differences else "possible_duplicate"
                reasons.append("Overlapping questionnaire/text evidence differs."
                               if differences else "Overlapping evidence may describe the same event.")
            elif "unknown" in relations:
                questionnaire_relation = "temporal_ambiguity"
                reasons.append("Questionnaire and text time windows cannot be aligned. "
                               "Preserve both without declaring a conflict or counting two events.")
        if reasons:
            decision = "needs_review"
            proposed = None
        elif proposed:
            decision = "eligible_for_automatic_acceptance"
        else:
            decision = "questionnaire_only"

        results.append(dict(
            checkin_id=checkin_id, measure=measure, evidence=evidence,
            parser_comparison=comparison, questionnaire_relation=questionnaire_relation,
            reconciliation_decision=decision,
            review_status="needs_review" if reasons else "pending",
            review_reason=" ".join(reasons) if reasons else None,
            validation_status="pending", proposed_observation=proposed,
        ))
    return results


def read_candidates(path, checkin_ids):
    groups = defaultdict(list)
    for row in load_artifact(path):
        if row["checkin_id"] in checkin_ids:
            groups[row["checkin_id"]].append(row)
    return groups


def write_json(path, data):
    with NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp") as temporary:
        temporary_path = Path(temporary.name)
    try:
        temporary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, help="Process the first N check-ins for a local smoke test")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as connection:
        checkins = connection.execute(
            "SELECT checkin_id, submitted_at, free_text FROM checkins ORDER BY checkin_id LIMIT ?",
            (args.limit if args.limit is not None else -1,),
        ).fetchall()

    ids = {row[0] for row in checkins}
    questionnaire = read_candidates(DATABASE_PATH.with_name("questionnaire_extractions.json"), ids)
    keyword = read_candidates(DATABASE_PATH.with_name("keyword_extractions.json"), ids)
    llm = extract_live(((key, report) for key, _, report in checkins), args.model)
    model_candidates = defaultdict(list)
    for row in llm["candidates"]:
        model_candidates[row["checkin_id"]].append(row)
    comparable_ids = set(llm["processed_checkin_ids"]) - llm["errors"].keys()
    results = []
    for key, submitted_at, report in checkins:
        results.extend(reconcile(
            key, submitted_at, report or "", questionnaire[key], keyword[key], model_candidates[key],
            llm_available=key in comparable_ids,
        ))
    comparisons = Counter(row["parser_comparison"] for row in results)
    decisions = Counter(row["reconciliation_decision"] for row in results)
    summary = dict(
        execution_mode=llm["execution_mode"], model=llm["model"],
        checkins_processed_by_llm=len(llm["processed_checkin_ids"]),
        valid_llm_candidates=len(llm["candidates"]), parser_agreements=comparisons["agreement"],
        parser_disagreements=comparisons["disagreement"], requiring_review=decisions["needs_review"],
        eligible_for_automatic_acceptance=decisions["eligible_for_automatic_acceptance"],
        parser_comparisons=dict(comparisons), failed_checkins=len(llm["errors"]),
    )
    write_json(DATABASE_PATH.with_name("llm_extractions.json"), llm)
    write_json(DATABASE_PATH.with_name("reconciliation_results.json"),
               {"summary": summary, "results": results})
    if llm.get("blocker"):
        print(llm["blocker"])
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
