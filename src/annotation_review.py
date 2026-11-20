"""Persistence and validation for independent human annotation review."""
import json
from copy import deepcopy
from datetime import date

from .evaluate import DEFAULT_REFERENCE, load, validate_reference, verified
from .reconcile_symptoms import write_json

PROGRESS_PATH = DEFAULT_REFERENCE.with_name("phase5_reference_reviewed.json")
EDIT_FIELDS = (
    "measure", "status", "severity_score", "frequency_count", "frequency_period",
    "original_phrase", "observed_date", "observed_time_text", "notes",
)


def make_demo_review(reference):
    """Create unreviewed working samples without changing the reference."""
    demo = deepcopy(reference)
    for section in ("cases", "duplicate_pairs"):
        for item in demo[section]:
            item.update(verification_status="draft", verified_by=None)
            if section == "duplicate_pairs":
                item["same_event"] = None
    return demo


def update_demo_review(review, section, index, original, edited):
    """Return an isolated session copy; use no file or database persistence."""
    if section not in ("cases", "duplicate_pairs"):
        raise ValueError("Unknown review section")
    if review[section][index] != original:
        raise ValueError("This sample changed. Reload the sample before saving.")
    identity = case_source if section == "cases" else pair_source
    if identity(edited) != identity(original):
        raise ValueError("Original report identity and text must be preserved.")
    result = deepcopy(review)
    result[section][index] = deepcopy(edited)
    return result


def case_source(case):
    return tuple(case[key] for key in ("checkin_id", "original_free_text", "submitted_at"))


def pair_source(pair):
    return tuple(pair["checkin_ids"]), pair["measure"]


def load_review(draft_path=DEFAULT_REFERENCE, progress_path=PROGRESS_PATH):
    draft = load(draft_path)
    if not progress_path.exists():
        return draft
    progress = load(progress_path)
    for key, identity in (("cases", case_source), ("duplicate_pairs", pair_source)):
        if list(map(identity, draft[key])) != list(map(identity, progress[key])):
            raise ValueError(f"Saved {key} no longer match the original draft.")
    return progress


def approval(confirm, reviewer):
    if not confirm:
        return {"verification_status": "draft", "verified_by": None}
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Enter a reviewer identifier before marking the item verified.")
    return {"verification_status": "verified", "verified_by": reviewer.strip()}


def edit_case(case, rows, confirm, reviewer):
    result = deepcopy(case)
    result.update(approval(confirm, reviewer))
    mentions = []
    for row in rows:
        mention = {field: row[field] for field in EDIT_FIELDS}
        mention["checkin_id"] = case["checkin_id"]
        phrase = mention["original_phrase"]
        if not isinstance(phrase, str) or not phrase.strip():
            raise ValueError("Enter the exact evidence text for each mention.")
        for field in ("frequency_period", "observed_date", "observed_time_text"):
            value = mention[field]
            if value is not None and (not isinstance(value, str) or not value
                                      or value not in phrase):
                raise ValueError(f"{field} must be literal wording inside the evidence span.")
        observed = mention["observed_date"]
        if observed and date.fromisoformat(observed).isoformat() != observed:
            raise ValueError("Use YYYY-MM-DD for an explicit occurrence date.")
        if mention["frequency_period"] and mention["frequency_count"] is None:
            raise ValueError("A frequency window requires a reported count.")
        mentions.append(mention)
    result["mentions"] = mentions
    validate_reference({"cases": [result]}, {case["checkin_id"]: (
        case["original_free_text"], case["submitted_at"],
    )})
    return result


def edit_pair(pair, label, notes, confirm, reviewer):
    if label is not None and type(label) is not bool:
        raise ValueError("Choose a valid pair decision.")
    if confirm and label is None:
        raise ValueError("Choose whether the reports share an occurrence before confirming.")
    return {**pair, "same_event": label, "notes": notes, **approval(confirm, reviewer)}


def save_item(section, index, original, edited, *, draft_path=DEFAULT_REFERENCE,
              progress_path=PROGRESS_PATH):
    if progress_path.resolve() == draft_path.resolve():
        raise ValueError("Progress must be saved separately from the original draft.")
    saved = load_review(draft_path, progress_path)
    if saved[section][index] != original:
        raise ValueError("This item changed in another session. Reload before saving.")
    identity = case_source if section == "cases" else pair_source
    if identity(edited) != identity(original):
        raise ValueError("Original report identity and text must be preserved.")
    saved[section][index] = edited
    write_json(progress_path, saved)
    return saved


def export_reference(review, reports):
    if not all(verified(case) for case in review["cases"]):
        raise ValueError("Complete all check-in reviews before exporting.")
    validate_reference(review, reports)
    return json.dumps(review, indent=2, ensure_ascii=False) + "\n"
