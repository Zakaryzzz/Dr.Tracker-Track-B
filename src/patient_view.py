"""Patient-focused Streamlit reporting view."""
import json
from contextlib import closing
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from .analysis import RAW, VALIDATED, coverage, daily_weight, open_readonly, patients, treatment_windows, weekly_vomiting
from .database import DATABASE_PATH
from .patient_summary import LABELS, latest_symptoms, medication_events, patient_reports, recent_evidence, score_rows, trend_summary, visit_comparison, vomiting_summary

REPORT_PATH = DATABASE_PATH.with_name("phase3c1_reconciliation.json")
COLORS = {"pain": "#136f70", "dizziness": "#8964a5", "fatigue": "#bc7730", "dry_mouth": "#467bc1"}


def severity_chart(scores, measures, events):
    figure = go.Figure()
    for measure in measures:
        values = [r for r in scores if r["measure"] == measure]
        figure.add_trace(go.Scatter(
            x=[r["submitted_at"] for r in values], y=[r["score"] for r in values],
            name=LABELS[measure], mode="lines+markers", connectgaps=False,
            line={"color": COLORS[measure], "width": 2},
            customdata=[[r["checkin_id"], r["availability"]] for r in values],
            hovertemplate="%{x}<br>" + LABELS[measure] + ": %{y}/10<br>%{customdata[0]}<extra></extra>",
        ))
        missing = [r for r in values if r["score"] is None]
        if missing:
            figure.add_trace(go.Scatter(
                x=[r["submitted_at"] for r in missing], y=[-0.45] * len(missing),
                mode="markers", name=LABELS[measure] + " · unavailable", showlegend=False,
                marker={"symbol": "x", "color": COLORS[measure], "size": 8},
                customdata=[[r["checkin_id"], r["availability"]] for r in missing],
                hovertemplate="%{x}<br>%{customdata[1]}<br>%{customdata[0]}<extra></extra>",
            ))
    for event in events:
        figure.add_shape(type="line", x0=event["event_date"], x1=event["event_date"],
                         y0=0, y1=1, yref="paper", line={"color": "#a6acb2", "dash": "dot"})
    if events:
        figure.add_trace(go.Scatter(
            x=[event["event_date"] for event in events], y=[10.1] * len(events),
            mode="markers", name="Medication event",
            marker={"symbol": "diamond", "size": 9, "color": "#667981"},
            customdata=[[event["treatment_name"], event["event_status"]] for event in events],
            hovertemplate="%{x}<br>%{customdata[0]} · %{customdata[1]}<extra></extra>",
        ))
    figure.update_layout(height=370, margin={"l": 15, "r": 15, "t": 20, "b": 20},
                         legend={"orientation": "h"}, hovermode="closest", template="plotly_white",
                         xaxis={"title": "Check-in submission date", "type": "date"},
                         yaxis={"title": "Severity (0–10)", "range": [-0.8, 10.3],
                                "tickvals": list(range(0, 11, 2))})
    return figure


def render_patient_dashboard():
    st.markdown("""
        <style>
        .block-container {max-width:1400px;padding-top:2rem;}
        h1,h2,h3 {color:#183d43;}
        [data-testid="stMetric"] {background:#f1f6f5;border-radius:10px;padding:16px;}
        </style>
    """, unsafe_allow_html=True)
    st.title("Dr.Tracker")
    st.caption("Patient reporting workspace · Synthetic demonstration data")
    if not DATABASE_PATH.is_file() or not REPORT_PATH.is_file():
        st.error("The dashboard requires the demo database and Phase 3C-1 reconciliation report.")
        return
    with closing(open_readonly()) as db:
        people = {r["patient_id"]: r for r in patients(db)}
        if not people:
            st.info("No patient records available.")
            return
        patient_id = st.sidebar.selectbox("Patient", list(people),
                                         format_func=lambda k: f"{k} · {people[k]['patient_name']}")
        patient = people[patient_id]
        reports = patient_reports(db, patient_id)
        if not reports:
            st.info("This patient has no reports yet.")
            return
        st.header(f"{patient['patient_name']} · {patient_id}")
        st.caption(f"Reporting period {reports[0]['submission_date']} – {reports[-1]['submission_date']} "
                   f"· Latest report {reports[-1]['submitted_at']} "
                   f"· Treatment days {reports[0]['treatment_day']}–{reports[-1]['treatment_day']}")
        layer = st.radio("Observation source", (RAW, VALIDATED), horizontal=True)
        scores = score_rows(db, patient_id, layer, reports)
        events = medication_events(db, patient_id)
        latest_day = reports[-1]["submission_date"]
        past_events = [e for e in events if e["event_date"] <= latest_day]
        state, biggest = trend_summary(scores, latest_day)
        status_col, symptoms_col = st.columns([1, 3])
        status_col.metric("Symptom trend", state)
        current = latest_symptoms(db, reports[-1], layer)
        symptoms_col.markdown("**Latest symptoms**")
        symptoms_col.write(" · ".join(current) if current else "No present or uncertain symptoms available in this source at the latest report.")
        st.caption(f"{layer}. Trend compares daily scores in the latest seven submission days with the "
                   "preceding seven. At least three scores per window are required. The largest absolute "
                   "mean change determines the label; changes under 1 point are stable. This is a display rule.")
        st.subheader("Clinical Summary")
        vomit = vomiting_summary(reports)
        cards = st.columns(4)
        cards[0].metric("Latest nausea severity", "Not collected")
        cards[0].caption("Nausea is absent from this questionnaire.")
        cards[1].metric("Vomiting · reported count sum", "Unavailable" if vomit["sum"] is None else str(vomit["sum"]))
        cards[1].caption(f"Raw · {vomit['start']} to {vomit['end']} · "
                         f"{vomit['numeric']}/{vomit['submissions']} numeric responses · "
                         f"{vomit['missing']} missing, {vomit['uncertain']} uncertain. Occurrence windows unknown.")
        cards[2].metric("Largest score change", f"{biggest['delta']:+.1f}/10" if biggest else "Unavailable")
        cards[2].caption(
            f"{LABELS[biggest['measure']]} · {biggest['before']:.1f} → {biggest['after']:.1f} "
            f"· n={biggest['before_n']}/{biggest['after_n']}" if biggest else "Insufficient numeric scores.")
        last_med = past_events[-1] if past_events else None
        cards[3].metric("Latest medication change", last_med["event_status"].capitalize() if last_med else "None recorded")
        cards[3].caption(f"{last_med['treatment_name']} · {last_med['event_date']}" if last_med else "No recorded medication event.")

        st.subheader("What Changed Since the Last Visit?")
        baseline, changes = visit_comparison(db, patient_id, reports, scores)
        with st.container(border=True):
            st.write(" ".join(changes) if changes else "No comparable numeric scores at the baseline and latest report.")
            st.caption(baseline)
            if biggest:
                st.write(f"Across the two latest seven-day submission windows, {LABELS[biggest['measure']].lower()} "
                         f"mean score changed from {biggest['before']:.1f}/10 to {biggest['after']:.1f}/10.")
            prior_end = (date.fromisoformat(latest_day) - timedelta(days=7)).isoformat()
            prior_vomiting = vomiting_summary(reports, prior_end)
            if prior_vomiting["sum"] is not None and vomit["sum"] is not None:
                st.write(f"Raw vomiting count sums: {prior_vomiting['sum']} in "
                         f"{prior_vomiting['start']}–{prior_vomiting['end']} "
                         f"({prior_vomiting['numeric']}/{prior_vomiting['submissions']} numeric responses), "
                         f"then {vomit['sum']} in {vomit['start']}–{vomit['end']} "
                         f"({vomit['numeric']}/{vomit['submissions']} numeric responses). "
                         "Reporting periods may overlap.")
            st.caption("Comparisons use submitted observations. Event timing and medication effects require separate evidence.")

        st.subheader("Symptom Trends")
        selected = st.multiselect("Symptoms", list(LABELS), default=["pain", "fatigue", "dry_mouth"],
                                  format_func=lambda key: LABELS[key])
        st.caption(layer)
        if patient_id == "P001" and layer == VALIDATED:
            st.info("P001 pain reports remain in reconciliation review. Select the raw source to view submitted pain scores.")
        if selected:
            st.plotly_chart(severity_chart(scores, selected, events), width="stretch")
            missing = sum(r["score"] is None for r in scores if r["measure"] in selected)
            st.caption(f"{missing} unavailable numeric observations in the selected series. "
                       "Crosses below zero indicate gaps and are not severity scores. "
                       "Dotted lines mark recorded medication events; details appear below.")
        else:
            st.info("Select at least one symptom to display.")

        st.subheader("Symptom Frequency")
        st.caption("Raw questionnaire submissions · Weekly vomiting reports")
        frequency = pd.DataFrame(weekly_vomiting(db, patient_id))
        if not frequency.empty:
            frequency["week_start"] = pd.to_datetime(frequency["week_start"])
            graph = px.bar(frequency, x="week_start", y="reported_count_sum",
                           labels={"week_start": "Submission week starting Monday",
                                   "reported_count_sum": "Sum of reported vomiting counts"},
                           color_discrete_sequence=["#136f70"])
            st.plotly_chart(graph, width="stretch")
            st.caption("Counts are reported values with unknown coverage periods. Overlapping reports can "
                       "describe the same occurrence. Blank weekly sums indicate no numeric answers.")
            st.dataframe(frequency.rename(columns={"positive": "Positive submissions",
                                                  "explicit_zero": "Zero submissions",
                                                  "uncertain": "Uncertain submissions",
                                                  "missing": "Missing submissions",
                                                  "reported_count_sum": "Reported count sum",
                                                  "submissions": "Total submissions"}).drop(columns="patient_id"),
                         hide_index=True, width="stretch")

        st.subheader("Medication Timeline")
        if events:
            frame = pd.DataFrame(events)
            frame["event_date"] = pd.to_datetime(frame["event_date"])
            timeline = px.scatter(frame, x="event_date", y="treatment_name", color="event_status",
                                  symbol="event_status", hover_data=["event_id", "treatment_day"],
                                  labels={"event_date": "Recorded medication date", "treatment_name": "Medication",
                                          "event_status": "Status"},
                                  color_discrete_map={"started": "#136f70", "adjusted": "#bc7730",
                                                      "stopped": "#946083", "completed": "#667981"})
            timeline.update_traces(marker_size=13)
            timeline.update_layout(height=230)
            st.plotly_chart(timeline, width="stretch")
            with st.expander("Before / after submission windows"):
                st.caption("Validated questionnaire observations · Seven submission days either side; event day excluded. "
                           "Latest submission per date; timestamp ties use check-in ID. Three numeric scores per side "
                           "are required for sufficient coverage. Temporal association does not establish causation.")
                st.dataframe(treatment_windows(db, patient_id), hide_index=True)
        else:
            st.info("No medication starts, stops, or adjustments recorded for this patient.")
        with st.expander("Weight measurements"):
            weights = pd.DataFrame(daily_weight(db, patient_id))
            if not weights.empty:
                weights["measured_date"] = pd.to_datetime(weights["measured_date"])
                chart = px.line(weights, x="measured_date", y="weight_lb", markers=True,
                                labels={"measured_date": "Measurement date", "weight_lb": "Weight (lb)"})
                if patient["weight_threshold_lb"] is not None:
                    chart.add_hline(y=patient["weight_threshold_lb"], line_dash="dash",
                                    annotation_text="Synthetic weight threshold")
                st.plotly_chart(chart, width="stretch")
                st.caption("One measurement per date, selected by latest linked submission, check-in ID, and weight ID.")
        groups = json.loads(REPORT_PATH.read_text(encoding="utf-8"))["results"]
        st.subheader("Recent Patient Reports")
        st.caption("Original statements and saved keyword / LLM candidates · Unresolved text remains evidence for review.")
        st.dataframe(recent_evidence(reports, groups), hide_index=True, width="stretch")
        with st.expander("Reporting quality and coverage"):
            stats = coverage(db, patient_id, REPORT_PATH)
            st.write(f"{stats['checkins']} submissions · {stats['validated_observations']} validated observations · "
                     f"{stats['unresolved_groups']} unresolved groups · {stats['text_agreements']} text-parser agreements")
            st.dataframe(pd.DataFrame(stats["missing_fields"].items(), columns=["Questionnaire field", "Missing"]),
                         hide_index=True)
