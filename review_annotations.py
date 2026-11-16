"""Local Streamlit interface for reviewing the Phase 5 reference."""
from contextlib import closing
from hashlib import sha256
import json

import pandas as pd
import streamlit as st

from src.analysis import open_readonly
from src.annotation_review import (
    EDIT_FIELDS, PROGRESS_PATH, edit_case, edit_pair, export_reference, load_review, save_item,
)
from src.evaluate import validate_reference, verified
from src.extract_symptoms import PATTERNS
from src.llm_extraction import STATUSES

PAIR_DECISIONS = {"Undecided": None, "Share an occurrence": True, "Distinct occurrences": False}


def revision(item):
    return sha256(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def show_report(case):
    st.caption(f"{case['checkin_id']} · Submitted {case['submitted_at']}")
    st.text(case["original_free_text"])
    for note in case.get("context_notes", []):
        st.caption(note)


def save_review(section, index, original, edited):
    save_item(section, index, original, edited)
    st.session_state["review_saved"] = "Review progress saved."
    st.rerun()


def editor_rows(frame):
    rows = []
    for row in frame.to_dict("records"):
        cleaned = {}
        for name, value in row.items():
            if pd.isna(value) or (isinstance(value, str) and not value.strip()):
                cleaned[name] = None
            elif name in ("severity_score", "frequency_count") and isinstance(value, float) and value.is_integer():
                cleaned[name] = int(value)
            else:
                cleaned[name] = value
        rows.append(cleaned)
    return rows


def review_case(cases, reviewer):
    st.subheader("Check-in review")
    indices = list(range(len(cases)))
    first_pending = next((i for i in indices if not verified(cases[i])), 0)
    index = st.selectbox("Check-in", indices, index=first_pending, key="case_navigation",
                         format_func=lambda i: f"{cases[i]['checkin_id']} · "
                         f"{'Verified' if verified(cases[i]) else 'Incomplete'}")
    case = cases[index]
    show_report(case)
    if verified(case):
        st.success(f"Verified by {case['verified_by']}")
    st.caption("Copy evidence verbatim from the report. Leave unsupported numbers and dates blank. "
               "Use the table controls to add or remove mentions.")
    frame = pd.DataFrame(case["mentions"], columns=EDIT_FIELDS)
    for name in ("severity_score", "frequency_count"):
        frame[name] = frame[name].astype("Int64")
    for name in EDIT_FIELDS:
        if name not in ("severity_score", "frequency_count"):
            frame[name] = frame[name].astype("string")
    form_key = f"case-{case['checkin_id']}-{revision(case)}"
    with st.form(form_key):
        edited = st.data_editor(
            frame, num_rows="dynamic", hide_index=True, width="stretch",
            column_config={
                "measure": st.column_config.SelectboxColumn("Symptom", options=sorted(PATTERNS), required=True),
                "status": st.column_config.SelectboxColumn("Status", options=list(STATUSES), required=True),
                "severity_score": st.column_config.NumberColumn("Severity (0–10)", min_value=0,
                                                               max_value=10, step=1),
                "frequency_count": st.column_config.NumberColumn("Count", min_value=0, step=1),
                "frequency_period": st.column_config.TextColumn("Count window"),
                "original_phrase": st.column_config.TextColumn("Exact evidence", required=True, width="large"),
                "observed_date": st.column_config.TextColumn("Explicit date (YYYY-MM-DD)"),
                "observed_time_text": st.column_config.TextColumn("Time wording"),
                "notes": st.column_config.TextColumn("Ambiguity notes"),
            },
        )
        confirm = st.checkbox("I independently checked all symptom mentions for this report.")
        draft_button, verify_button = st.columns(2)
        save_draft = draft_button.form_submit_button("Save as incomplete")
        save_verified = verify_button.form_submit_button("Save verified review", type="primary")
    if save_draft or save_verified:
        try:
            if save_verified and not confirm:
                raise ValueError("Confirm that you checked all symptom mentions before verifying.")
            updated = edit_case(case, editor_rows(edited), save_verified, reviewer)
            save_review("cases", index, case, updated)
        except (ValueError, TypeError, OSError) as error:
            st.error(str(error))


def review_pairs(pairs, cases, reviewer):
    st.subheader("Duplicate-event review")
    if not pairs:
        st.info("There are no duplicate-event pairs in this sample.")
        return
    indices = list(range(len(pairs)))
    index = st.selectbox("Check-in pair", indices, key="pair_navigation",
                         format_func=lambda i: f"{' / '.join(pairs[i]['checkin_ids'])} · "
                         f"{'Verified' if verified(pairs[i]) else 'Incomplete'}")
    pair = pairs[index]
    st.caption(f"Symptom under review · {pair['measure']}")
    case_by_id = {case["checkin_id"]: case for case in cases}
    for column, key in zip(st.columns(2), pair["checkin_ids"]):
        with column:
            show_report(case_by_id[key])
    st.caption("Choose shared occurrence if the reports share at least one occurrence. "
               "Record any additional distinct occurrence in the notes.")
    options = list(PAIR_DECISIONS)
    selected = next(i for i, label in enumerate(PAIR_DECISIONS.values()) if label is pair["same_event"])
    with st.form(f"pair-{index}-{revision(pair)}"):
        label = st.radio("Relationship", options, index=selected)
        notes = st.text_area("Pair notes", value=pair.get("notes") or "")
        confirm = st.checkbox("I independently checked both reports and the selected relationship.")
        draft_button, verify_button = st.columns(2)
        save_draft = draft_button.form_submit_button("Save pair as incomplete")
        save_verified = verify_button.form_submit_button("Save verified pair", type="primary")
    if save_draft or save_verified:
        try:
            if save_verified and not confirm:
                raise ValueError("Confirm the relationship before verifying this pair.")
            updated = edit_pair(pair, PAIR_DECISIONS[label], notes or None, save_verified, reviewer)
            save_review("duplicate_pairs", index, pair, updated)
        except (ValueError, TypeError, OSError) as error:
            st.error(str(error))


def main():
    st.set_page_config(page_title="Dr.Tracker · Annotation review", layout="wide")
    st.title("Annotation review")
    st.caption("Dr.Tracker · Phase 5 · Synthetic patient reports")
    try:
        review = load_review()
        with closing(open_readonly()) as db:
            reports = {key: (text, timestamp) for key, text, timestamp in db.execute(
                "SELECT checkin_id, free_text, submitted_at FROM checkins"
            )}
        validate_reference(review, reports)
    except (OSError, ValueError, KeyError) as error:
        st.error(f"Could not load the review set: {error}")
        return
    cases, pairs = review["cases"], review["duplicate_pairs"]
    completed = sum(verified(case) for case in cases)
    completed_pairs = sum(verified(pair) for pair in pairs)
    st.progress(completed / len(cases), text=f"{completed}/{len(cases)} check-ins verified")
    st.caption(f"{completed_pairs}/{len(pairs)} duplicate-event pairs verified · Progress saves to {PROGRESS_PATH.name}")
    if message := st.session_state.pop("review_saved", None):
        st.success(message)
    reviewer = st.sidebar.text_input("Reviewer identifier", key="reviewer")
    view = st.sidebar.radio("Review", ("Check-ins", "Duplicate pairs"))
    if view == "Check-ins":
        review_case(cases, reviewer)
    else:
        review_pairs(pairs, cases, reviewer)
    if completed == len(cases):
        if completed_pairs != len(pairs):
            st.info("Some duplicate pairs are incomplete. Duplicate detection metrics remain pending.")
        st.download_button("Export reviewed reference", export_reference(review, reports),
                           file_name="phase5_reference_verified.json", mime="application/json")


if __name__ == "__main__":
    main()
