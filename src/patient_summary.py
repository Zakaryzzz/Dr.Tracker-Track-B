"""Patient-scoped reporting summaries used by the clinical dashboard."""
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from .analysis import RAW, VALIDATED, VALIDATED_SCORES, records
from .extract_symptoms import FLAGS, SCORES, map_questionnaire

LABELS = {"pain": "Pain", "dizziness": "Dizziness", "fatigue": "Fatigue", "dry_mouth": "Dry mouth"}


def reports_to_clarify(reports, groups, keyword, llm, audit):
    """Select patient reports from saved extraction evidence without claiming full coverage."""
    by_id = defaultdict(list)
    for group in groups:
        by_id[group["checkin_id"]].append(group)
    saved = defaultdict(list)
    for candidate in [*keyword, *llm.get("candidates", [])]:
        saved[candidate["checkin_id"]].append(candidate)
    result = []
    for report in sorted(reports, key=lambda r: (r["submitted_at"], r["checkin_id"]), reverse=True):
        text, key = report.get("free_text"), report["checkin_id"]
        if not text or not text.strip():
            continue
        reasons, details = set(), []
        candidates = list(saved[key])
        answers = {row["measure"]: row for row in map_questionnaire(key, report["raw_answers"])}
        for group in by_id[key]:
            for source in ("keyword", "llm"):
                candidates.extend(group["evidence"].get(source, []))
            codes = set(group.get("review_reasons", []))
            if codes.intersection({"parser_claim_mismatch"}):
                reasons.add("Conflicting extractions")
            if codes.intersection({"one_parser_only", "llm_unavailable", "temporal_ambiguity"}):
                reasons.add("Unmatched evidence")
            if "possible_duplicate" in codes:
                reasons.add("Possible repeat")
            if "unsupported_evidence" in codes or group["evidence"].get("rejected_llm_observations"):
                reasons.add("Extraction needs checking")
            details.extend(group.get("review_reasons", []))
        if not candidates:
            reasons.add("Unclassified")
        if key in llm.get("errors", {}) or key in llm.get("initial_validation_errors", {}):
            reasons.add("Extraction failed")
            details.append(llm.get("errors", {}).get(key) or llm["initial_validation_errors"][key])
        for attempt in audit.get(key, {}).get("attempts", {}).values():
            if attempt.get("rejected"):
                reasons.add("Extraction needs checking")
                details.extend(row["reason"] for row in attempt["rejected"])
            candidates.extend(attempt.get("valid", []))
        for candidate in candidates:
            if candidate.get("rejected_fields"):
                reasons.add("Extraction needs checking")
                details.extend(f"{field}: {item['reason']}" for field, item in candidate["rejected_fields"].items())
            if candidate.get("status") == "uncertain":
                reasons.add("Uncertain")
            answer = answers.get(candidate.get("measure"))
            if answer:
                disagreement = answer["status"] != candidate.get("status") or any(
                    answer.get(field) is not None and candidate.get(field) is not None
                    and answer[field] != candidate[field] for field in ("severity_score", "frequency_count")
                )
                if disagreement:
                    reasons.add("Different answers · timing unknown")
        # Literal span coverage is a triage cue only. Uncovered wording may
        # be context, an unrecognized symptom, or an anaphoric reference.
        covered = [False] * len(text)
        for candidate in candidates:
            phrase = candidate.get("original_phrase")
            if not isinstance(phrase, str) or not phrase:
                continue
            start = text.find(phrase)
            while start >= 0:
                covered[start:start + len(phrase)] = [True] * len(phrase)
                start = text.find(phrase, start + len(phrase))
        remainder = "".join(" " if covered[i] else char for i, char in enumerate(text))
        words = re.findall(r"\w+", remainder.lower())
        if any(word not in {"and", "but", "or", "i", "my", "the", "a", "an"} for word in words):
            reasons.add("Coverage unconfirmed")
        if reasons:
            result.append({
                "checkin_id": key, "submitted_at": report["submitted_at"],
                "free_text": text, "reasons": sorted(reasons), "details": sorted(set(details)),
            })
    # Reports are source submissions. Repeated input rows cannot duplicate a card.
    return list({row["checkin_id"]: row for row in result}.values())


def numeric(value):
    return type(value) is int and 0 <= value <= 10


def latest_symptoms(db, report, layer):
    if layer == RAW:
        answers = report["answers"]
        current = [
            f"{LABELS[measure]} {answers[measure + '_score']}/10"
            for measure in SCORES if numeric(answers.get(measure + "_score"))
            and answers[measure + "_score"] > 0
        ]
        current += [key.replace("_", " ").capitalize() for key in FLAGS if answers.get(key) is True]
        current += [key.replace("_score", "").replace("_", " ").capitalize() + " uncertain"
                    for key, value in answers.items() if value == "uncertain"]
        count = answers.get("vomiting_count")
        if type(count) is int and count > 0:
            current.append(f"Vomiting count {count} · window unknown")
        return current
    if layer != VALIDATED:
        raise ValueError("Unknown observation layer")
    return [
        row["measure"].replace("_", " ").capitalize() + " · " + row["status"]
        + (f" {row['severity_score']}/10" if row["severity_score"] is not None else "")
        for row in records(db, """
            SELECT measure, status, severity_score FROM symptoms
            WHERE checkin_id = ? AND source_type = 'questionnaire'
              AND validation_status = 'passed' AND status IN ('present', 'uncertain')
            ORDER BY measure
        """, (report["checkin_id"],))
    ]


def patient_reports(db, patient_id):
    rows = records(db, """
        SELECT checkin_id, submitted_at, treatment_day, raw_answers, free_text
        FROM checkins WHERE patient_id = ? ORDER BY submitted_at, checkin_id
    """, (patient_id,))
    for row in rows:
        row["answers"] = json.loads(row["raw_answers"])
        row["submission_date"] = row["submitted_at"][:10]
    return rows


def score_rows(db, patient_id, layer, reports):
    if layer not in (RAW, VALIDATED):
        raise ValueError("Unknown observation layer")
    stored = {}
    if layer == VALIDATED:
        stored = {(row["checkin_id"], row["measure"]): row["score"] for row in records(db, f"""
            SELECT v.* FROM ({VALIDATED_SCORES}) v
            JOIN checkins c ON c.checkin_id = v.checkin_id WHERE c.patient_id = ?
        """, (patient_id,))}
    result = []
    for report in reports:
        for measure in SCORES:
            value = (report["answers"].get(measure + "_score") if layer == RAW
                     else stored.get((report["checkin_id"], measure)))
            result.append({
                "checkin_id": report["checkin_id"], "submitted_at": report["submitted_at"],
                "submission_date": report["submission_date"], "measure": measure,
                "symptom": LABELS[measure], "score": value if numeric(value) else None,
                "availability": ("Recorded" if numeric(value) else
                                 "Uncertain" if value == "uncertain" else "No numeric observation"),
            })
    return result


def daily_scores(rows):
    latest = {}
    for row in sorted(rows, key=lambda item: (item["submitted_at"], item["checkin_id"])):
        latest[row["submission_date"], row["measure"]] = row
    return list(latest.values())


def top_symptom_changes(db, patient_id, reports, scores, comparison="7-day windows"):
    """Rank current numeric scores and compare only explicitly available reports."""
    if comparison not in ("7-day windows", "Last two visits"):
        raise ValueError("Unknown comparison period")
    latest = reports[-1]
    ranked = sorted(
        (row for row in scores if row["checkin_id"] == latest["checkin_id"] and row["score"] is not None),
        key=lambda row: (-row["score"], list(LABELS).index(row["measure"])),
    )[:3]
    period = "Latest 7 submission days vs preceding 7"
    visit_reports = []
    if comparison == "Last two visits":
        visits = records(db, """
            SELECT DISTINCT event_date FROM treatments WHERE patient_id = ?
            AND event_type = 'follow_up' AND event_date <= ?
            ORDER BY event_date DESC LIMIT 2
        """, (patient_id, latest["submission_date"]))
        for visit in reversed(visits):
            visit_reports.append(next(
                (report for report in reversed(reports) if report["submission_date"] <= visit["event_date"]), None
            ))
        period = ("Two recorded visits are required." if len(visits) < 2 else
                  f"Visit dates {visits[1]['event_date']} → {visits[0]['event_date']}")
    index = {(row["checkin_id"], row["measure"]): row["score"] for row in scores}
    result = []
    for row in ranked:
        before = after = delta = None
        state = "Insufficient Data"
        if comparison == "7-day windows":
            state, change = trend_summary(
                [item for item in scores if item["measure"] == row["measure"]], latest["submission_date"]
            )
            if change:
                before, after, delta = change["before"], change["after"], change["delta"]
        elif (len(visit_reports) == 2 and all(visit_reports)
              and visit_reports[0]["checkin_id"] != visit_reports[1]["checkin_id"]):
            before, after = [index[report["checkin_id"], row["measure"]] for report in visit_reports]
            if before is not None and after is not None:
                delta = after - before
                state = "Stable" if abs(delta) < 1 else "Worsening" if delta > 0 else "Improving"
        result.append({**row, "before": before, "after": after, "delta": delta, "status": state})
    return result, period


def trend_summary(rows, end_date):
    end = date.fromisoformat(end_date)
    current_start, previous_start = end - timedelta(days=6), end - timedelta(days=13)
    changes = []
    for measure in SCORES:
        before, after = [], []
        for row in daily_scores(rows):
            if row["measure"] != measure or row["score"] is None:
                continue
            day = date.fromisoformat(row["submission_date"])
            if previous_start <= day < current_start:
                before.append(row["score"])
            elif current_start <= day <= end:
                after.append(row["score"])
        if len(before) >= 3 and len(after) >= 3:
            a, b = mean(before), mean(after)
            changes.append({"measure": measure, "before": a, "after": b, "delta": b-a,
                            "before_n": len(before), "after_n": len(after)})
    if not changes:
        return "Insufficient Data", None
    biggest = max(changes, key=lambda item: abs(item["delta"]))
    # The label describes the largest measured change; it is a display rule.
    status = "Stable" if abs(biggest["delta"]) < 1 else "Worsening" if biggest["delta"] > 0 else "Improving"
    return status, biggest


def vomiting_summary(reports, end_date=None):
    end = date.fromisoformat(end_date or reports[-1]["submission_date"])
    start = end - timedelta(days=6)
    selected = [r for r in reports if start.isoformat() <= r["submission_date"] <= end.isoformat()]
    values = [r["answers"].get("vomiting_count") for r in selected]
    numeric_values = [v for v in values if type(v) is int and v >= 0]
    return {"start": start.isoformat(), "end": end.isoformat(),
            "sum": sum(numeric_values) if numeric_values else None,
            "numeric": len(numeric_values), "submissions": len(values),
            "missing": sum(v is None for v in values), "uncertain": sum(v == "uncertain" for v in values)}


def medication_events(db, patient_id):
    return records(db, """
        SELECT t.event_id, t.event_date, t.treatment_name, t.event_status, t.treatment_day,
               t.medication_frequency, t.medication_details_source,
               c.free_text AS reported_details
        FROM treatments t LEFT JOIN checkins c
          ON c.checkin_id = t.checkin_id AND c.patient_id = t.patient_id
        WHERE t.patient_id = ? AND t.event_type = 'medication'
        ORDER BY t.event_date, t.event_id
    """, (patient_id,))


def visit_comparison(db, patient_id, reports, scores):
    latest = reports[-1]
    visit = db.execute("""
        SELECT event_date FROM treatments WHERE patient_id = ?
        AND event_type = 'follow_up' AND event_date < ?
        ORDER BY event_date DESC, event_id DESC LIMIT 1
    """, (patient_id, latest["submission_date"])).fetchone()
    baseline = None
    if visit:
        baseline = next((r for r in reversed(reports) if r["submission_date"] <= visit[0]), None)
    if baseline:
        context = f"Baseline is the latest submission on or before follow-up dated {visit[0]}."
    elif len(reports) >= 2:
        baseline = reports[-2]
        context = "No prior dated follow-up has a usable report. Comparing the two latest submissions."
    else:
        return "Insufficient reports for comparison.", []
    index = {(r["checkin_id"], r["measure"]): r["score"] for r in scores}
    changes = []
    for measure in SCORES:
        a = index[baseline["checkin_id"], measure]
        b = index[latest["checkin_id"], measure]
        if a is not None and b is not None:
            changes.append(f"{LABELS[measure]} {a}/10 → {b}/10 ({b-a:+d}).")
    context += f" {baseline['submitted_at']} → {latest['submitted_at']}."
    return context, changes


def recent_evidence(reports, groups, limit=6):
    by_id = {}
    for group in groups:
        by_id.setdefault(group["checkin_id"], []).append(group)
    rows = []
    for report in reversed(reports[-limit:]):
        mentions, severity, frequency, status = [], [], [], []
        for group in by_id.get(report["checkin_id"], []):
            for source in ("keyword", "llm"):
                for row in group["evidence"][source]:
                    name = row["measure"].replace("_", " ")
                    mentions.append(f"{source}: {name} ({row['status']})")
                    if row.get("severity_score") is not None:
                        severity.append(f"{source} {name}: {row['severity_score']}/10")
                    if row.get("frequency_count") is not None:
                        period = row.get("frequency_period") or "window unspecified"
                        frequency.append(f"{source} {name}: {row['frequency_count']} · {period}")
                    if row.get("rejected_fields"):
                        status.append(f"{source}: rejected optional fields")
            if group["review_status"] == "needs_review":
                status.append(f"{group['measure']}: needs review")
        rows.append({"Date": report["submitted_at"], "Check-in": report["checkin_id"],
                     "Original patient statement": report["free_text"] or "No statement",
                     "Extracted symptoms": "; ".join(mentions) or "No retained text candidates",
                     "Severity": "; ".join(severity) or "Not extracted",
                     "Frequency": "; ".join(frequency) or "Not extracted",
                     "Uncertainty / validation": "; ".join(dict.fromkeys(status)) or "No unresolved text group"})
    return rows
