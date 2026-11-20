"""Phase 3C-1 offline review of the saved Phase 3B model responses."""
import json
import re
from collections import Counter, defaultdict
from contextlib import closing
from uuid import NAMESPACE_URL, uuid5

from .database import DATABASE_PATH, connect
from .identifiers import identifier, legacy_checkin, load_artifact
from .extract_symptoms import map_questionnaire, parse_keywords
from .llm_extraction import unique_fields, validate_response
from .reconcile_symptoms import (
    claim, measure_of, normalize, read_candidates, time_window, write_json,
)


def source_span(report, quote):
    """Return offsets only for an unambiguous, literal quote."""
    if not isinstance(quote, str) or not quote:
        return None
    start = report.find(quote)
    if start < 0 or report.find(quote, start + 1) >= 0:
        return None
    return start, start + len(quote)


def same_mention(report, left, right):
    a, b = (source_span(report, row.get("original_phrase")) for row in (left, right))
    return bool(a and b and ((a[0] <= b[0] and b[1] <= a[1])
                            or (b[0] <= a[0] and a[1] <= b[1])))


def corroborated(row, report, references):
    """Use contextual rule evidence and accept only explicitly grounded fields."""
    for reference in references:
        if measure_of(reference) != measure_of(row) or not same_mention(report, row, reference):
            continue
        # Compare required status and any asserted optional claims. A parser
        # may omit a field, but cannot transfer a claim from another symptom.
        if row["status"] != reference["status"]:
            continue
        if any(value is not None and value != expected
               for value, expected in zip(claim(row)[1:], claim(reference)[1:])):
            # The keyword parser leaves frequency_period unset. A named
            # window is supported if it is also the scoped time of the count.
            stripped = {**row, "frequency_period": None}
            if (row.get("frequency_period") is None or row.get("frequency_count") is None
                    or normalize(row["frequency_period"]) != normalize(reference.get("observed_time_text"))
                    or any(value is not None and value != expected
                           for value, expected in zip(claim(stripped)[1:], claim(reference)[1:]))):
                continue
        if any(row.get(field) is not None and normalize(row[field]) != normalize(reference.get(field))
               for field in ("observed_date", "observed_time_text")):
            continue
        return True
    return False


def validate_attempt(raw, key, report, references):
    """Validate each observation independently, including contextual grounding."""
    accepted, rejected = [], []
    try:
        payload = json.loads(raw, object_pairs_hook=unique_fields)
        if (not isinstance(payload, dict) or set(payload) != {"observations"}
                or not isinstance(payload["observations"], list)):
            raise ValueError("Expected an observations array")
    except (ValueError, TypeError) as error:
        return accepted, [{"reason": str(error)}], False
    for index, original in enumerate(payload["observations"]):
        try:
            row, = validate_response(json.dumps({"observations": [original]}),
                                     key, report, retain_partial=True)
            if not corroborated(row, report, references):
                raise ValueError("Claim not corroborated by the quoted symptom in context")
        except (ValueError, TypeError) as error:
            rejected.append({"index": index, "original_observation": original, "reason": str(error)})
        else:
            accepted.append(row)
    return accepted, rejected, True


def replay_attempts(log, key, report, references):
    attempts = {}
    for name, field in (("initial", "raw_responses"), ("retry", "retry_responses")):
        if key in log.get(field, {}):
            valid, rejected, parsed = validate_attempt(log[field][key], key, report, references)
            attempts[name] = {"valid": valid, "rejected": rejected, "parsed": parsed}
    if not attempts:
        return [], {"selected_attempt": None, "attempts": attempts}, False
    # Keep one attempt per report, preserving all other evidence in the
    # audit. Combining attempts could count the same mention twice.
    selected = max(attempts, key=lambda name: (
        len(attempts[name]["valid"]), attempts[name]["parsed"],
        -len(attempts[name]["rejected"]),
        -sum(len(row.get("rejected_fields", {})) for row in attempts[name]["valid"]),
        name == "retry",
    ))
    chosen = attempts[selected]
    available = chosen["parsed"] and bool(chosen["valid"] or not chosen["rejected"])
    return chosen["valid"], {"selected_attempt": selected, "attempts": attempts}, available


def observation(row, kind, report=None):
    fields = ("checkin_id", "measure", "status", "severity_score", "frequency_count",
              "frequency_period", "pain_location", "observed_date", "observed_time_text",
              "original_phrase", "source_type", "extraction_method")
    result = {field: row.get(field) for field in fields}
    span = source_span(report, row.get("original_phrase")) if report is not None else None
    key = json.dumps([legacy_checkin(row["checkin_id"]), measure_of(row), kind, span], separators=(",", ":"))
    result.update(observation_id=identifier("symptoms", str(uuid5(NAMESPACE_URL, "drtracker:" + key))),
                  observation_type=kind, evidence_span=span)
    return result


def compare_group(key, measure, report, answers, keyword, model, references, available, rejected):
    reasons = set()
    valid_rules = [row for row in keyword if corroborated(row, report, references)]
    if (len(valid_rules) != len(keyword) or rejected
            or any(row.get("rejected_fields") for row in model)):
        reasons.add("unsupported_evidence")
    text = bool(keyword or model or rejected)
    parser_status = "no_text_candidate"
    shared = None
    if text and not available:
        parser_status = "llm_unavailable"
        reasons.add(parser_status)
    elif text and (not valid_rules or not model):
        parser_status = "one_parser_only"
        reasons.add(parser_status)
    elif text and (len(valid_rules) != 1 or len(model) != 1):
        parser_status = "multiple_mentions"
        reasons.add("temporal_ambiguity")
    elif text:
        left, right = valid_rules[0], model[0]
        if not same_mention(report, left, right):
            parser_status = "unmatched_spans"
            reasons.add("temporal_ambiguity")
        elif any(a is not None and b is not None and a != b
                 for a, b in zip(claim(left), claim(right))) or any(
                     left.get(field) and right.get(field)
                     and normalize(left[field]) != normalize(right[field])
                     for field in ("observed_date", "observed_time_text")
                 ):
            parser_status = "claim_mismatch"
            reasons.add("parser_claim_mismatch")
        else:
            parser_status = "agreement"
            shared = {**left, "extraction_method": "reconciled"}

    # Current questionnaire answers have no documented reporting window.
    relationship = "time_unknown" if answers and text else "not_applicable"
    if relationship == "time_unknown":
        reasons.add("questionnaire_time_unknown")
    candidates = [observation(row, "questionnaire") for row in answers]
    for candidate in candidates:
        candidate["validation_status"] = "passed"
    text_observations = [observation(row, "free_text", report) for row in valid_rules + model]
    if shared:
        shared = observation(shared, "free_text", report)
        shared["validation_status"] = "passed"
    return {
        "checkin_id": key, "measure": measure, "text_parser_status": parser_status,
        "questionnaire_text_relationship": relationship, "review_reasons": sorted(reasons),
        "questionnaire_observations": candidates, "supported_text_candidates": text_observations,
        "agreed_text_observation": shared,
        "evidence": {"questionnaire": answers, "keyword": keyword, "llm": model,
                     "rejected_llm_observations": rejected},
        "possible_duplicate_links": [], "distinct_occurrence_links": [],
    }


SAME_EPISODE = re.compile(r"\b(?:same|that same) (?:\w+ ){0,3}(?:spell|episode)\b", re.I)
EARLIER_REFERENCE = re.compile(r"\b(?:first|earlier) (?:spell|episode) was (before breakfast|this morning)\b", re.I)


def link_checkins(groups, checkins):
    by_patient_measure = defaultdict(list)
    for group in groups:
        patient, submitted, report, _ = checkins[group["checkin_id"]]
        seen_windows = set()
        for item in group["supported_text_candidates"]:
            window = time_window(item, submitted)
            if window and window[1] and window not in seen_windows:
                seen_windows.add(window)
                by_patient_measure[patient, group["measure"]].append((submitted, window, group, item, report))
    for mentions in by_patient_measure.values():
        mentions.sort(key=lambda mention: mention[0])
        for index, (_, window, group, item, report) in enumerate(mentions):
            # Use explicit reference language and require an unambiguous earlier match.
            earlier = [entry for entry in mentions[:index] if entry[2] is not group]
            if SAME_EPISODE.search(item["original_phrase"]):
                matches = [entry for entry in earlier if entry[1] == window]
                if len(matches) == 1:
                    other = matches[0][2]
                    for current, peer in ((group, other), (other, group)):
                        current["possible_duplicate_links"].append(peer["checkin_id"])
                        current["review_reasons"] = sorted(set(current["review_reasons"]) | {"possible_duplicate"})
            if window[1] == "evening" and EARLIER_REFERENCE.search(report):
                matches = [entry for entry in earlier if entry[1] == (window[0], "morning")]
                if len(matches) == 1:
                    group["distinct_occurrence_links"].append({
                        "checkin_id": matches[0][2]["checkin_id"],
                        "relationship": "distinct_evening_mention_with_reference_to_morning",
                    })


def main():
    def load(name):
        return load_artifact(DATABASE_PATH.with_name(name))

    with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as connection:
        checkins = {key: (patient, submitted, report or "", raw)
                    for key, patient, submitted, report, raw in connection.execute(
                        "SELECT checkin_id, patient_id, submitted_at, free_text, raw_answers "
                        "FROM checkins ORDER BY checkin_id")}
    questionnaire = read_candidates(DATABASE_PATH.with_name("questionnaire_extractions.json"), checkins)
    keyword = read_candidates(DATABASE_PATH.with_name("keyword_extractions.json"), checkins)
    log = load("llm_extractions.json")
    results, replay = [], {}
    for key, (_, _, report, raw) in checkins.items():
        if questionnaire[key] != map_questionnaire(key, raw):
            raise ValueError(f"Questionnaire export differs from SQLite for {key}")
        references = parse_keywords(key, report)
        model, audit, available = replay_attempts(log, key, report, references)
        replay[key] = audit
        chosen = audit["attempts"].get(audit["selected_attempt"], {})
        rejected = chosen.get("rejected", [])
        sources = defaultdict(lambda: [[], [], [], []])
        for index, rows in enumerate((questionnaire[key], keyword[key], model)):
            for row in rows:
                sources[measure_of(row)][index].append(row)
        for row in rejected:
            original = row.get("original_observation")
            if isinstance(original, dict) and isinstance(original.get("measure"), str):
                measure = measure_of(original)
                if measure in sources:
                    sources[measure][3].append(row)
        for measure, evidence in sorted(sources.items()):
            results.append(compare_group(key, measure, report, *evidence[:3], references,
                                         available, evidence[3]))
    link_checkins(results, checkins)
    counts = Counter()
    reasons = Counter()
    for group in results:
        review = group["review_reasons"]
        eligible = []
        if not review:
            if group["text_parser_status"] == "agreement":
                eligible = [group["agreed_text_observation"]]
            elif group["text_parser_status"] == "no_text_candidate":
                eligible = group["questionnaire_observations"]
        group["insertion_candidates"] = eligible
        group["insertion_eligibility"] = "eligible" if eligible else "held_for_review"
        group["review_status"] = "needs_review" if review else "pending"
        reasons.update(review)
        counts["valid_questionnaire_observations"] += len(group["questionnaire_observations"])
        counts["supported_free_text_candidates"] += len(group["supported_text_candidates"])
        counts["text_parser_agreements"] += group["text_parser_status"] == "agreement"
        counts["questionnaire_text_timing_uncertainties"] += "questionnaire_time_unknown" in review
        counts["groups_with_possible_duplicates"] += bool(group["possible_duplicate_links"])
        counts["candidates_eligible_for_insertion"] += len(eligible)
        counts["groups_requiring_human_review"] += bool(review)
    counts["checkins"] = len(checkins)
    counts["groups"] = len(results)
    counts["supported_keyword_candidates"] = sum(
        row["extraction_method"] == "keyword"
        for group in results for row in group["supported_text_candidates"]
    )
    counts["supported_saved_llm_candidates"] = (
        counts["supported_free_text_candidates"] - counts["supported_keyword_candidates"]
    )
    counts["rejected_observations_across_saved_attempts"] = sum(
        len(attempt["rejected"]) for audit in replay.values() for attempt in audit["attempts"].values()
    )
    summary = {"execution_mode": "offline_saved_responses", "saved_model": log.get("model"),
               "counts": dict(counts), "overlapping_review_reason_counts": dict(reasons)}
    write_json(DATABASE_PATH.with_name("phase3c1_reconciliation.json"),
               {"summary": summary, "results": results, "llm_revalidation_audit": replay})
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
