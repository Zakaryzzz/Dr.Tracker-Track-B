"""Local Streamlit interface for reviewing the Phase 5 reference."""
from contextlib import closing
from hashlib import sha256
import json

import pandas as pd
import streamlit as st

from src.analysis import open_readonly
from src.annotation_review import (
    EDIT_FIELDS, PROGRESS_PATH, edit_case, edit_pair, export_reference, load_review, save_item,
    make_demo_review, update_demo_review,
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


def save_review(section, index, original, edited, *, demo=False):
    if demo:
        st.session_state["annotation_demo"] = update_demo_review(
            st.session_state["annotation_demo"], section, index, original, edited
        )
        st.session_state["demo_message"] = "Demo progress updated for this session."
    else:
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


def review_case(cases, reviewer, *, read_only=False, demo=False):
    st.subheader("Check-in review")
    indices = list(range(len(cases)))
    first_pending = next((i for i in indices if not verified(cases[i])), 0)
    index = st.selectbox("Check-in", indices, index=first_pending,
                         key="demo_case_navigation" if demo else "case_navigation",
                         format_func=lambda i: f"{cases[i]['checkin_id']} · "
                         f"{'Reviewed in demo' if demo and verified(cases[i]) else 'Verified' if verified(cases[i]) else 'Needs review'}")
    case = cases[index]
    show_report(case)
    if verified(case):
        st.success("Reviewed in this demo session." if demo else f"Verified by {case['verified_by']}")
    if read_only:
        if case["mentions"]:
            st.dataframe(pd.DataFrame(case["mentions"], columns=EDIT_FIELDS), hide_index=True)
        else:
            st.info("No symptom mentions in the verified reference for this report.")
        return
    st.caption("Copy evidence verbatim from the report. Leave unsupported numbers and dates blank. "
               "Use the table controls to add or remove mentions.")
    frame = pd.DataFrame(case["mentions"], columns=EDIT_FIELDS)
    for name in ("severity_score", "frequency_count"):
        frame[name] = frame[name].astype("Int64")
    for name in EDIT_FIELDS:
        if name not in ("severity_score", "frequency_count"):
            frame[name] = frame[name].astype("string")
    prefix = f"demo-{st.session_state.get('demo_generation', 0)}" if demo else "case"
    form_key = f"{prefix}-{case['checkin_id']}-{revision(case)}"
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
        confirm = st.checkbox("I checked all mentions in this demo sample." if demo
                              else "I independently checked all symptom mentions for this report.")
        draft_button, verify_button = st.columns(2)
        save_draft = draft_button.form_submit_button("Keep pending in demo" if demo else "Save as incomplete")
        save_verified = verify_button.form_submit_button("Complete demo review" if demo else "Save verified review", type="primary")
    if save_draft or save_verified:
        try:
            if save_verified and not confirm:
                raise ValueError("Confirm that you checked all symptom mentions before verifying.")
            updated = edit_case(case, editor_rows(edited), save_verified, reviewer)
            save_review("cases", index, case, updated, demo=demo)
        except (ValueError, TypeError, OSError) as error:
            st.error(str(error))


def review_pairs(pairs, cases, reviewer, *, read_only=False, demo=False):
    st.subheader("Duplicate-event review")
    if not pairs:
        st.info("There are no duplicate-event pairs in this sample.")
        return
    indices = list(range(len(pairs)))
    index = st.selectbox("Check-in pair", indices, key="demo_pair_navigation" if demo else "pair_navigation",
                         format_func=lambda i: f"{' / '.join(pairs[i]['checkin_ids'])} · "
                         f"{'Reviewed in demo' if demo and verified(pairs[i]) else 'Verified' if verified(pairs[i]) else 'Needs review'}")
    pair = pairs[index]
    st.caption(f"Symptom under review · {pair['measure']}")
    case_by_id = {case["checkin_id"]: case for case in cases}
    for column, key in zip(st.columns(2), pair["checkin_ids"]):
        with column:
            show_report(case_by_id[key])
    if read_only:
        label = next(name for name, value in PAIR_DECISIONS.items() if value is pair["same_event"])
        st.write(f"Relationship · {label}")
        if verified(pair):
            st.success(f"Verified by {pair['verified_by']}")
        if pair.get("notes"):
            st.write(pair["notes"])
        return
    st.caption("Choose shared occurrence if the reports share at least one occurrence. "
               "Record any additional distinct occurrence in the notes.")
    options = list(PAIR_DECISIONS)
    selected = next(i for i, label in enumerate(PAIR_DECISIONS.values()) if label is pair["same_event"])
    prefix = f"demo-pair-{st.session_state.get('demo_generation', 0)}" if demo else "pair"
    with st.form(f"{prefix}-{index}-{revision(pair)}"):
        label = st.radio("Relationship", options, index=selected)
        notes = st.text_area("Pair notes", value=pair.get("notes") or "")
        confirm = st.checkbox("I checked both reports and the relationship in this demo." if demo
                              else "I independently checked both reports and the selected relationship.")
        draft_button, verify_button = st.columns(2)
        save_draft = draft_button.form_submit_button("Keep pair pending in demo" if demo else "Save pair as incomplete")
        save_verified = verify_button.form_submit_button("Complete demo pair review" if demo else "Save verified pair", type="primary")
    if save_draft or save_verified:
        try:
            if save_verified and not confirm:
                raise ValueError("Confirm the relationship before verifying this pair.")
            updated = edit_pair(pair, PAIR_DECISIONS[label], notes or None, save_verified, reviewer)
            save_review("duplicate_pairs", index, pair, updated, demo=demo)
        except (ValueError, TypeError, OSError) as error:
            st.error(str(error))


def show_review(*, read_only=False):
    st.title("Annotation review")
    st.caption("Dr.Tracker · Phase 5 · Synthetic patient reports")
    try:
        review = (json.loads(PROGRESS_PATH.with_name("phase5_reference_verified_demo.json")
                             .read_text(encoding="utf-8")) if read_only else load_review())
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
    if read_only:
        st.caption(f"{completed_pairs}/{len(pairs)} duplicate-event pairs verified · Read-only demo")
        st.caption("These verified labels are used for extraction evaluation. Operational reconciliation is separate.")
        view = st.radio("Review", ("Check-ins", "Duplicate pairs"), horizontal=True)
        if view == "Check-ins":
            review_case(cases, "", read_only=True)
        else:
            review_pairs(pairs, cases, "", read_only=True)
        return
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


def show_review_demo():
    st.title("Annotation Review")
    st.caption("Interactive demo · Changes stay in this browser session")
    if "annotation_demo" not in st.session_state:
        draft = json.loads(PROGRESS_PATH.with_name("phase5_reference_draft.json").read_text(encoding="utf-8"))
        st.session_state["annotation_demo"] = make_demo_review(draft)
    if st.button("Reset demo"):
        generation = st.session_state.get("demo_generation", 0) + 1
        for key in list(st.session_state):
            if key == "annotation_demo" or key.startswith("demo"):
                del st.session_state[key]
        st.session_state["demo_generation"] = generation
        st.rerun()
    review = st.session_state["annotation_demo"]
    cases, pairs = review["cases"], review["duplicate_pairs"]
    completed = sum(verified(case) for case in cases)
    st.progress(completed / len(cases), text=f"{completed}/{len(cases)} samples reviewed in demo")
    st.caption(f"{sum(verified(pair) for pair in pairs)}/{len(pairs)} pairs reviewed in demo")
    if message := st.session_state.pop("demo_message", None):
        st.success(message)
    view = st.radio("Review", ("Check-ins", "Duplicate pairs"), horizontal=True, key="demo_view")
    if view == "Check-ins":
        review_case(cases, "Demo reviewer", demo=True)
    else:
        review_pairs(pairs, cases, "Demo reviewer", demo=True)


def main():
    st.set_page_config(page_title="Dr.Tracker · Annotation review", layout="wide")
    show_review()


if __name__ == "__main__":
    main()
