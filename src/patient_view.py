"""Patient-focused Streamlit reporting view."""
import json
from contextlib import closing

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from .analysis import RAW, daily_weight, open_readonly, patients, treatment_windows
from .database import DATABASE_PATH
from .extract_symptoms import FLAGS
from .patient_summary import numeric, reports_to_clarify, LABELS, medication_events, patient_reports, recent_evidence, score_rows, top_symptom_changes

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


def show_clarification_panel(items, patient_id):
    st.subheader("Reports to Clarify")
    if not items:
        st.caption("No reports flagged by the saved extraction records.")
        return
    st.caption(f"{len(items)} reports · newest first")
    page_count = (len(items) + 3) // 4
    page = st.selectbox("Report page", range(page_count), key=f"clarify-page-{patient_id}",
                        format_func=lambda index: f"{index + 1} of {page_count}") if page_count > 1 else 0
    with st.container(height=440, border=True):
        for item in items[page * 4:(page + 1) * 4]:
            st.caption(item["submitted_at"])
            st.markdown(" · ".join(f"**{reason}**" for reason in item["reasons"]))
            st.text(item["free_text"])
            with st.expander("Evidence details", expanded=False):
                st.caption(item["checkin_id"])
                for detail in item["details"]:
                    st.write(detail)
                if "Coverage unconfirmed" in item["reasons"]:
                    st.write("Some wording lies outside saved evidence spans; it may be context or an unrecognized symptom.")
            st.divider()


def recent_questionnaire_rows(reports):
    rows = []
    for report in reversed(reports[-6:]):
        answers = report["answers"]
        flags = [name.replace("_", " ").capitalize() for name in FLAGS if answers.get(name) is True]
        uncertain = [name.replace("_score", "").replace("_", " ").capitalize()
                     for name, value in answers.items() if value == "uncertain"]
        scores = []
        for measure, label in LABELS.items():
            value = answers.get(measure + "_score")
            scores.append(f"{label}: {value}/10" if numeric(value) else
                          f"{label}: uncertain" if value == "uncertain" else f"{label}: not reported")
        count = answers.get("vomiting_count")
        rows.append({
            "Submitted": report["submitted_at"], "Patient statement": report["free_text"] or "No statement",
            "Scores": "; ".join(scores), "Other symptoms": ", ".join(flags) or "No positive answers",
            "Vomiting count": str(count) if type(count) is int and count >= 0 else
                              "Uncertain" if count == "uncertain" else "Not reported",
            "Uncertain answers": ", ".join(uncertain) or "None",
            "Unanswered fields": sum(answers.get(key) is None for key in
                                      (*FLAGS, *(measure + "_score" for measure in LABELS), "vomiting_count")),
        })
    return rows


def render_patient_dashboard():
    st.markdown("""
        <style>
        .block-container {max-width:1480px;padding-top:2rem;}
        h1,h2,h3 {color:#183d43;}
        [data-testid="stMetric"] {background:#f1f6f5;border-radius:10px;padding:16px;}
        </style>
    """, unsafe_allow_html=True)
    st.title("Dr.Tracker")
    st.caption("Patient-reported symptoms · Synthetic demonstration")
    required = [DATABASE_PATH, REPORT_PATH, DATABASE_PATH.with_name("keyword_extractions.json"),
                DATABASE_PATH.with_name("llm_extractions.json")]
    if any(not path.is_file() for path in required):
        st.error("The dashboard requires the demo database and saved extraction reports.")
        return
    reconciliation = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    keyword = json.loads(required[2].read_text(encoding="utf-8"))
    llm = json.loads(required[3].read_text(encoding="utf-8"))
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
        st.caption(f"{reports[0]['submission_date']} – {reports[-1]['submission_date']} "
                   f"· Latest report {reports[-1]['submitted_at']} "
                   f"· Treatment days {reports[0]['treatment_day']}–{reports[-1]['treatment_day']}")
        scores = score_rows(db, patient_id, RAW, reports)
        events = medication_events(db, patient_id)
        st.subheader("Most Severe Symptoms")
        comparison = st.selectbox("Compare changes", ("7-day windows", "Last two visits"))
        top, period = top_symptom_changes(db, patient_id, reports, scores, comparison)
        st.caption(f"Latest questionnaire scores · {period}")
        if top:
            for column, item in zip(st.columns(len(top)), top):
                with column:
                    st.metric(LABELS[item["measure"]], f"{item['score']}/10",
                              delta=f"{item['delta']:+.1f} points" if item["delta"] is not None else None,
                              delta_color="inverse")
                    if item["delta"] is not None:
                        st.caption(f"{item['status']} · {item['before']:.1f} → {item['after']:.1f}/10")
                    else:
                        st.caption("Insufficient comparison data")
        else:
            st.info("No numeric symptom scores in the latest questionnaire.")

        graph_col, clarify_col = st.columns([2.4, 1], gap="large")
        items = reports_to_clarify(reports, reconciliation["results"], keyword, llm,
                                   reconciliation.get("llm_revalidation_audit", {}))
        with graph_col:
            st.subheader("Symptom Trends")
            selected = st.multiselect("Symptoms", list(LABELS), default=["pain", "fatigue", "dry_mouth"],
                                      format_func=lambda key: LABELS[key])
            if selected:
                st.plotly_chart(severity_chart(scores, selected, events), width="stretch")
                missing = sum(r["score"] is None for r in scores if r["measure"] in selected)
                st.caption(f"{missing} missing or uncertain scores · × indicates no numeric score · ◆ medication event")
            else:
                st.info("Select at least one symptom to display.")
        with clarify_col:
            show_clarification_panel(items, patient_id)

        st.subheader("Medication Timeline")
        if events:
            frame = pd.DataFrame(events)
            frame["event_date"] = pd.to_datetime(frame["event_date"])
            frame["Schedule"] = "Not recorded"
            frame["Dose"] = "Not recorded"
            frame["Patient report"] = frame["reported_details"].fillna("No linked patient statement")
            timeline = px.scatter(
                frame, x="event_date", y="treatment_name", color="event_status", symbol="event_status",
                custom_data=["treatment_name", "event_status", "Schedule", "Dose", "Patient report"],
                color_discrete_map={"started": "#136f70", "adjusted": "#bc7730",
                                    "stopped": "#946083", "completed": "#667981"},
                labels={"event_date": "Recorded date", "treatment_name": "", "event_status": "Status"},
            )
            timeline.update_traces(
                marker_size=13,
                hovertemplate=("<b>%{customdata[0]}</b><br>Date: %{x|%b %d, %Y}"
                               "<br>Status: %{customdata[1]}<br>Schedule: %{customdata[2]}"
                               "<br>Dose: %{customdata[3]}<br>Patient report: %{customdata[4]}<extra></extra>"),
            )
            timeline.update_layout(height=250, yaxis_title=None)
            st.plotly_chart(timeline, width="stretch")
            with st.expander("Scores before / after changes"):
                st.caption("Patient questionnaire scores · Seven submission days either side, excluding the event day. "
                           "Latest submission per date; at least three scores per side. Association does not establish causation.")
                comparisons = pd.DataFrame(treatment_windows(db, patient_id, layer=RAW))
                if not comparisons.empty:
                    comparisons = comparisons.drop(columns=["event_id"])
                    comparisons = comparisons.rename(columns={"treatment_name": "Medication"})
                    comparisons = comparisons[["Medication"] + [name for name in comparisons if name != "Medication"]]
                    st.dataframe(comparisons, hide_index=True)
        else:
            st.caption("No medication changes recorded.")
        st.subheader("Recent Patient Reports")
        st.dataframe(recent_questionnaire_rows(reports), hide_index=True, width="stretch")
        with st.expander("Extraction evidence and review status"):
            st.dataframe(recent_evidence(reports, reconciliation["results"]), hide_index=True, width="stretch")

        with st.expander("Weight measurements"):
            weights = pd.DataFrame(daily_weight(db, patient_id))
            if not weights.empty:
                weights["measured_date"] = pd.to_datetime(weights["measured_date"])
                chart = px.line(weights, x="measured_date", y="weight_lb", markers=True,
                                labels={"measured_date": "Measurement date", "weight_lb": "Weight (lb)"})
                if patient["weight_threshold_lb"] is not None:
                    chart.add_hline(y=patient["weight_threshold_lb"], line_dash="dash",
                                    annotation_text="Synthetic threshold")
                st.plotly_chart(chart, width="stretch")
