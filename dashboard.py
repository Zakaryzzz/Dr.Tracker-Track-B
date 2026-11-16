"""Streamlit dashboard for the synthetic Dr.Tracker dataset."""
from contextlib import closing
import json

import pandas as pd
import plotly.express as px
import streamlit as st

from src.analysis import (
    MIN_WINDOW_SCORES, RAW, VALIDATED, WEIGHT, coverage, daily_weight,
    open_readonly, pain_trend, patients, treatment_windows, weekly_vomiting,
)
from src.database import DATABASE_PATH

REPORT_PATH = DATABASE_PATH.with_name("phase3c1_reconciliation.json")


def show_pain(db, patient_id, layer, axis):
    data = pd.DataFrame(pain_trend(db, patient_id, layer))
    if data.empty or not data["pain_score"].notna().any():
        st.info(f"No numeric pain scores for {patient_id} in this layer. "
                "Choose the raw questionnaire layer to inspect submitted answers.")
        return
    x = "submitted_at" if axis == "Submission date" else "treatment_day"
    if x == "submitted_at":
        data[x] = pd.to_datetime(data[x], format="ISO8601")
    chart = px.line(data, x=x, y="pain_score", markers=True,
                    hover_data=["checkin_id"], labels={x: axis, "pain_score": "Pain score"})
    chart.update_traces(connectgaps=False, line_color="#176b67")
    chart.update_yaxes(range=[-0.25, 10.25], tick0=0, dtick=2)
    if x == "submitted_at":
        chart.update_xaxes(tickformat="%b %d")
    st.plotly_chart(chart)


def show_vomiting(db, patient_id):
    st.subheader("Weekly vomiting reports")
    st.caption(RAW)
    data = pd.DataFrame(weekly_vomiting(db, patient_id))
    if data.empty:
        st.info("No questionnaire submissions for this patient.")
        return
    labels = {"positive": "Positive", "explicit_zero": "Explicit zero",
              "uncertain": "Uncertain", "missing": "Missing"}
    counts = data.rename(columns=labels).melt(
        id_vars="week_start", value_vars=list(labels.values()),
        var_name="Response", value_name="Submissions",
    )
    chart = px.bar(counts, x="week_start", y="Submissions", color="Response",
                   category_orders={"Response": list(labels.values())},
                   color_discrete_sequence=["#b5552b", "#176b67", "#8a66a7", "#9ba8af"],
                   labels={"week_start": "Submission week starting Monday"})
    st.plotly_chart(chart)
    st.caption("Each bar counts submissions. Questionnaire reporting windows are unknown.")
    with st.expander("Reported counts by week"):
        st.dataframe(data[["week_start", "reported_count_sum", "submissions"]],
                     hide_index=True, column_config={"week_start": "Week starting Monday",
                     "reported_count_sum": "Sum of reported counts", "submissions": "Submissions"})
        st.caption("The sum includes numeric answers only. Multiple submissions can cover the same period.")


def show_weight(db, patient):
    st.subheader("Weight trend")
    st.caption(WEIGHT)
    data = pd.DataFrame(daily_weight(db, patient["patient_id"]))
    if data.empty:
        st.info("No recorded weight measurements.")
        return
    data["measured_date"] = pd.to_datetime(data["measured_date"], format="%Y-%m-%d")
    chart = px.line(data, x="measured_date", y="weight_lb", markers=True,
                    hover_data=["submitted_at", "measurements_that_day"],
                    labels={"measured_date": "Measurement date", "weight_lb": "Weight (lb)"})
    chart.update_traces(line_color="#176b67")
    threshold = patient["weight_threshold_lb"]
    if threshold is not None:
        chart.add_hline(y=threshold, line_dash="dash", line_color="#a0532f",
                        annotation_text=f"Synthetic threshold · {threshold:g} lb")
    st.plotly_chart(chart)
    if threshold is None:
        st.caption("No synthetic weight threshold is configured for this patient.")
    st.caption("One point per measurement date, using the latest linked check-in timestamp. "
               "Ties use check-in ID, then weight ID, in descending order.")


def show_coverage(stats):
    st.subheader("Data quality and coverage")
    st.caption("Selected patient · questionnaire, SQLite, and Phase 3C-1 review report")
    for column, (label, value) in zip(st.columns(4), (
        ("Raw answered fields", stats["raw_answered_fields"]),
        ("Validated observations", stats["validated_observations"]),
        ("Unresolved groups", stats["unresolved_groups"]),
        ("Free-text agreements", stats["text_agreements"]),
    )):
        column.metric(label, value)
    st.caption("Free-text agreements remain outside the validated observation layer.")
    with st.expander(f"Missing questionnaire answers · {sum(stats['missing_fields'].values())}"):
        st.dataframe(pd.DataFrame(stats["missing_fields"].items(), columns=["Field", "Missing"]),
                     hide_index=True)


def show_treatment_changes(db, patient_id):
    st.subheader("Reports around treatment changes")
    st.caption(VALIDATED)
    rows = treatment_windows(db, patient_id)
    if not rows:
        st.info("No recorded treatment changes for this patient.")
        return
    st.write("Seven submission days before and after each change. The event day is excluded. "
             "Each date uses its latest submission, with check-in ID breaking timestamp ties.")
    st.caption(f"Sufficient coverage requires {MIN_WINDOW_SCORES} numeric scores per window. "
               "These comparisons describe reports around a change and establish no causal effect.")
    data = pd.DataFrame(rows).drop(columns=["event_id"])
    st.dataframe(data, hide_index=True, column_config={
        "before_mean": st.column_config.NumberColumn("Before mean", format="%.2f"),
        "after_mean": st.column_config.NumberColumn("After mean", format="%.2f"),
    })


def show_saved_extraction(db):
    checkin_id = "demo-P001-01-1"
    source = db.execute("SELECT free_text FROM checkins WHERE checkin_id = ?", (checkin_id,)).fetchone()
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    example = next((group for group in report["results"]
                    if group["checkin_id"] == checkin_id and group["measure"] == "pain"
                    and group["evidence"]["llm"]), None)
    if source is None or example is None:
        return

    with st.expander("Saved Ollama output · Qwen2.5:3b"):
        st.caption(f"Synthetic example · {checkin_id} · Captured during a local run")
        original, extracted = st.columns(2)
        original.markdown("**Original check-in**")
        original.text(source["free_text"])
        extracted.markdown("**Saved extraction**")
        extracted.json(example["evidence"]["llm"], expanded=True)
        st.markdown("**Reconciliation**")
        st.text(f"Parser status: {example['text_parser_status']}\n"
                f"Review status: {example['review_status']}\n"
                f"Insertion eligibility: {example['insertion_eligibility']}")
        if example["review_reasons"]:
            st.write(", ".join(example["review_reasons"]))


def main():
    st.set_page_config(page_title="Dr.Tracker · Patient reports", page_icon="◈", layout="wide")
    st.title("Dr.Tracker")
    st.caption("Track B · Synthetic patient-reported data")
    if not DATABASE_PATH.is_file() or not REPORT_PATH.is_file():
        st.error("The Phase 4 dashboard requires drtracker.db and phase3c1_reconciliation.json in data/.")
        return
    with closing(open_readonly()) as db:
        choices = {row["patient_id"]: row for row in patients(db)}
        if not choices:
            st.info("No patients available.")
            return
        patient_id = st.sidebar.selectbox("Patient", list(choices),
                                          format_func=lambda key: f"{key} · {choices[key]['patient_name']}")
        patient = choices[patient_id]
        stats = coverage(db, patient_id, REPORT_PATH)
        days = stats["treatment_days"]
        st.header(patient["patient_name"])
        context = f"Treatment start {patient['treatment_start_date']} · {stats['checkins']} submissions"
        if days:
            context += f" · Treatment days {days[0]}–{days[-1]}"
        st.caption(context)
        st.subheader("Pain severity trend")
        controls = st.columns(2)
        layer = controls[0].radio("Pain data layer", (VALIDATED, RAW))
        axis = controls[1].radio("Pain chart horizontal axis", ("Submission date", "Treatment day"),
                                 horizontal=True)
        st.caption(f"{layer} · Scores follow check-in submission dates or treatment days. "
                   "Symptom occurrence windows are unknown.")
        if patient_id == "P001" and layer == VALIDATED:
            st.info("P001’s pain groups remain in reconciliation review, so this layer has no pain "
                    "scores. Select Raw questionnaire submissions to view P001’s submitted trend.")
        else:
            show_pain(db, patient_id, layer, axis)
        left, right = st.columns(2)
        with left:
            show_vomiting(db, patient_id)
        with right:
            show_weight(db, patient)
        show_treatment_changes(db, patient_id)
        show_coverage(stats)
        show_saved_extraction(db)


if __name__ == "__main__":
    main()
