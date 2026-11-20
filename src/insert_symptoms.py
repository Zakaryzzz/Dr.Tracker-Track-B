"""Insert only validated Phase 3C-1 questionnaire observations."""
import argparse
import json
import sqlite3
from contextlib import closing

from .database import DATABASE_PATH, connect
from .identifiers import load_artifact
from .extract_symptoms import map_questionnaire
from .reconcile_phase3c import observation

REPORT_PATH = DATABASE_PATH.with_name("phase3c1_reconciliation.json")
COLUMNS = (
    "observation_id", "checkin_id", "measure", "status", "severity_score",
    "frequency_count", "frequency_period", "value_text", "original_phrase",
    "observed_date", "observed_time_text", "source_type", "pain_location",
    "related_event_id", "extraction_method", "possible_duplicate_group",
    "review_status", "review_reason", "validation_status",
)
SELECT_ROW = f"SELECT {', '.join(COLUMNS)} FROM symptoms WHERE observation_id = ?"
INSERT_ROW = (
    f"INSERT INTO symptoms ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in COLUMNS)})"
)


def validate_batch(connection, report):
    """Re-derive every eligible candidate from its original questionnaire."""
    rows = []
    seen = set()
    excluded = 0
    questionnaire_by_checkin = {}
    for group in report["results"]:
        key = group["checkin_id"]
        measure = group["measure"]
        group_key = (key, measure)
        if group_key in seen:
            raise ValueError(f"Duplicate report group: {group_key}")
        seen.add(group_key)

        eligibility = group["insertion_eligibility"]
        candidates = group["insertion_candidates"]
        if eligibility == "held_for_review":
            if candidates:
                raise ValueError(f"Held group contains insertion candidates: {group_key}")
            excluded += 1
            continue
        evidence = group["evidence"]
        if (eligibility != "eligible" or group["review_status"] != "pending"
                or group["text_parser_status"] != "no_text_candidate"
                or group["questionnaire_text_relationship"] != "not_applicable"
                or group["review_reasons"] or group["possible_duplicate_links"]
                or group["supported_text_candidates"] or group["agreed_text_observation"] is not None
                or any(evidence[name] for name in ("keyword", "llm", "rejected_llm_observations"))
                or not isinstance(candidates, list) or len(candidates) != 1):
            raise ValueError(f"Group is not an eligible questionnaire-only observation: {group_key}")

        if key not in questionnaire_by_checkin:
            source = connection.execute(
                "SELECT c.raw_answers FROM checkins AS c "
                "JOIN patients AS p ON p.patient_id = c.patient_id WHERE c.checkin_id = ?",
                (key,),
            ).fetchone()
            if source is None:
                raise ValueError(f"Missing check-in or associated patient: {key}")
            questionnaire_by_checkin[key] = {
                row["measure"]: row for row in map_questionnaire(key, source[0])
            }
        answer = questionnaire_by_checkin[key].get(measure)
        if answer is None:
            raise ValueError(f"No explicit questionnaire answer: {group_key}")
        if answer["pain_location"] is not None and not isinstance(answer["pain_location"], str):
            raise ValueError(f"Invalid questionnaire pain location: {group_key}")
        expected = observation(answer, "questionnaire")
        expected["validation_status"] = "passed"

        # Exact typed comparison also checks the stable ID and NULL reporting
        # window, and rejects values such as false or 0.0 in integer fields.
        def matches(left, right):
            return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

        if not (matches(candidates, [expected])
                and matches(group["questionnaire_observations"], [expected])
                and matches(evidence["questionnaire"], [answer])):
            raise ValueError(f"Candidate does not match the source questionnaire: {group_key}")
        expected["review_status"] = "pending"
        rows.append(tuple(expected.get(column) for column in COLUMNS))
    return rows, excluded


def process(connection, report, mode="insert", *, expected_count=None):
    """Classify the batch and optionally insert it within one transaction."""
    with connection:
        connection.execute("BEGIN IMMEDIATE" if mode == "insert" else "BEGIN")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise ValueError("Foreign key enforcement is disabled")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign key check failed")

        rows, excluded = validate_batch(connection, report)
        if expected_count is not None and len(rows) != expected_count:
            raise ValueError(f"Expected {expected_count} Phase 3C-1 candidates, found {len(rows)}")
        missing = []
        for row in rows:
            existing = connection.execute(SELECT_ROW, (row[0],)).fetchone()
            if existing is None:
                missing.append(row)
            elif existing != row:
                raise ValueError(f"Conflicting existing observation: {row[0]}")

        if mode == "verify" and missing:
            raise ValueError(f"{len(missing)} eligible observations are missing")
        if mode == "insert":
            connection.executemany(INSERT_ROW, missing)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Database foreign key check failed after insertion")

        return {
            "mode": mode, "eligible": len(rows), "excluded_review_groups": excluded,
            "already_identical": len(rows) - len(missing),
            "would_insert": len(missing) if mode == "dry-run" else 0,
            "inserted": len(missing) if mode == "insert" else 0,
            "symptoms_total": connection.execute("SELECT COUNT(*) FROM symptoms").fetchone()[0],
            "foreign_key_check": "passed",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    mode = "dry-run" if args.dry_run else "verify" if args.verify else "insert"
    try:
        report = load_artifact(REPORT_PATH)
        uri = DATABASE_PATH.as_uri() + ("?mode=rw" if mode == "insert" else "?mode=ro")
        with closing(connect(uri, uri=True)) as connection:
            result = process(connection, report, mode, expected_count=1460)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        parser.exit(1, f"Symptom import failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
