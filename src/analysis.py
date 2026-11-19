"""Read-only SQL and coverage data for the Phase 4 dashboard."""
import json
import sqlite3

from .database import DATABASE_PATH, connect
from .extract_symptoms import FLAGS, SCORES

RAW = "Raw questionnaire submissions"
VALIDATED = "Validated questionnaire observations"
WEIGHT = "Recorded weight measurements"
MIN_WINDOW_SCORES = 3

# A repeated validated key is ambiguous. Exclude it instead of multiplying
# submissions or choosing one of the conflicting observations.
VALIDATED_SCORES = """
SELECT checkin_id, measure, MAX(severity_score) AS score
FROM symptoms
WHERE source_type = 'questionnaire' AND validation_status = 'passed'
GROUP BY checkin_id, measure
HAVING COUNT(*) = 1
"""

WEEKLY_VOMITING = """
WITH answers AS (
    SELECT patient_id, date(substr(submitted_at, 1, 10), '-6 days', 'weekday 1') AS week_start,
           json_type(raw_answers, '$.vomiting_count') AS kind,
           json_extract(raw_answers, '$.vomiting_count') AS answer
    FROM checkins WHERE patient_id = ?
)
SELECT patient_id, week_start,
       SUM(kind IS 'integer' AND answer > 0) AS positive,
       SUM(kind IS 'integer' AND answer = 0) AS explicit_zero,
       SUM(answer IS 'uncertain' AND kind IS 'text') AS uncertain,
       SUM(kind IS NULL OR kind = 'null') AS missing,
       SUM(CASE WHEN kind = 'integer' AND answer >= 0 THEN answer END) AS reported_count_sum,
       COUNT(*) AS submissions
FROM answers GROUP BY patient_id, week_start ORDER BY week_start
"""

TREATMENT_WINDOWS = f"""
WITH validated AS ({VALIDATED_SCORES}),
measures(measure) AS (VALUES ('pain'), ('dizziness'), ('fatigue'), ('dry_mouth')),
ranked AS (
    SELECT checkin_id, substr(submitted_at, 1, 10) AS submission_date,
           ROW_NUMBER() OVER (
               PARTITION BY substr(submitted_at, 1, 10)
               ORDER BY submitted_at DESC, checkin_id DESC
           ) AS daily_rank
    FROM checkins WHERE patient_id = :patient
),
daily AS (
    SELECT checkin_id, submission_date FROM ranked WHERE daily_rank = 1
),
windows AS (
    SELECT t.event_id, t.event_date, t.treatment_name, t.event_status, m.measure,
           CAST(julianday(d.submission_date) - julianday(t.event_date) AS INTEGER) AS day_offset,
           v.score
    FROM treatments t CROSS JOIN measures m
    LEFT JOIN daily d ON julianday(d.submission_date) - julianday(t.event_date) BETWEEN -7 AND 7
    LEFT JOIN validated v ON v.checkin_id = d.checkin_id AND v.measure = m.measure
    WHERE t.patient_id = :patient
      AND lower(t.event_status) IN ('adjusted', 'started', 'stopped', 'changed')
),
counts AS (
    SELECT event_id, event_date, treatment_name, event_status, measure,
           COUNT(CASE WHEN day_offset BETWEEN -7 AND -1 THEN 1 END) AS before_submissions,
           COUNT(CASE WHEN day_offset BETWEEN 1 AND 7 THEN 1 END) AS after_submissions,
           COUNT(CASE WHEN day_offset BETWEEN -7 AND -1 THEN score END) AS before_scores,
           COUNT(CASE WHEN day_offset BETWEEN 1 AND 7 THEN score END) AS after_scores,
           AVG(CASE WHEN day_offset BETWEEN -7 AND -1 THEN score END) AS before_mean,
           AVG(CASE WHEN day_offset BETWEEN 1 AND 7 THEN score END) AS after_mean
    FROM windows GROUP BY event_id, event_date, treatment_name, event_status, measure
)
SELECT *, CASE WHEN before_scores >= :minimum AND after_scores >= :minimum
               THEN 'Sufficient coverage' ELSE 'Insufficient data' END AS coverage
FROM counts ORDER BY event_date, event_id, measure
"""

DAILY_WEIGHT = """
WITH ranked AS (
    SELECT w.weight_id, w.measured_date, w.weight_lb, c.submitted_at,
           COUNT(*) OVER (PARTITION BY w.measured_date) AS measurements_that_day,
           ROW_NUMBER() OVER (
               PARTITION BY w.measured_date
               ORDER BY c.submitted_at DESC, c.checkin_id DESC, w.weight_id DESC
           ) AS daily_rank
    FROM weight w JOIN checkins c
      ON c.checkin_id = w.checkin_id AND c.patient_id = w.patient_id
    WHERE w.patient_id = ? AND w.weight_lb > 0
)
SELECT weight_id, measured_date, weight_lb, submitted_at, measurements_that_day
FROM ranked WHERE daily_rank = 1 ORDER BY measured_date
"""


def open_readonly(path=DATABASE_PATH):
    db = connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def records(db, sql, parameters=()):
    return [dict(row) for row in db.execute(sql, parameters)]


def patients(db):
    return records(db, "SELECT * FROM patients ORDER BY patient_id")


def pain_trend(db, patient_id, layer):
    if layer == RAW:
        score = """CASE WHEN json_type(c.raw_answers, '$.pain_score') = 'integer'
                         AND json_extract(c.raw_answers, '$.pain_score') BETWEEN 0 AND 10
                        THEN json_extract(c.raw_answers, '$.pain_score') END"""
        join = ""
    elif layer == VALIDATED:
        score = "v.score"
        join = f"LEFT JOIN ({VALIDATED_SCORES}) v ON v.checkin_id = c.checkin_id AND v.measure = 'pain'"
    else:
        raise ValueError(f"Unknown pain data layer: {layer}")
    return records(db, f"""
        SELECT c.checkin_id, c.submitted_at, substr(c.submitted_at, 1, 10) AS submission_date,
               c.treatment_day, {score} AS pain_score
        FROM checkins c {join}
        WHERE c.patient_id = ? ORDER BY c.submitted_at, c.checkin_id
    """, (patient_id,))


def weekly_vomiting(db, patient_id):
    return records(db, WEEKLY_VOMITING, (patient_id,))


def treatment_windows(db, patient_id, layer=VALIDATED):
    if layer not in (RAW, VALIDATED):
        raise ValueError("Unknown observation layer")
    query = TREATMENT_WINDOWS
    if layer == RAW:
        raw_scores = """
            SELECT c.checkin_id, m.measure,
                   CASE WHEN json_type(c.raw_answers, '$.' || m.measure || '_score') = 'integer'
                          AND json_extract(c.raw_answers, '$.' || m.measure || '_score') BETWEEN 0 AND 10
                        THEN json_extract(c.raw_answers, '$.' || m.measure || '_score') END AS score
            FROM checkins c
            CROSS JOIN (SELECT 'pain' AS measure UNION ALL SELECT 'dizziness'
                        UNION ALL SELECT 'fatigue' UNION ALL SELECT 'dry_mouth') m
            WHERE c.patient_id = :patient
        """
        query = query.replace(VALIDATED_SCORES, raw_scores)
    return records(db, query, {"patient": patient_id, "minimum": MIN_WINDOW_SCORES})


def daily_weight(db, patient_id):
    return records(db, DAILY_WEIGHT, (patient_id,))


def coverage(db, patient_id, report_path):
    checkins = records(db, "SELECT checkin_id, treatment_day, raw_answers FROM checkins WHERE patient_id = ?",
                       (patient_id,))
    ids = {row["checkin_id"] for row in checkins}
    groups = json.loads(report_path.read_text(encoding="utf-8"))["results"]
    keys = (*FLAGS, *(f"{measure}_score" for measure in SCORES), "vomiting_count")
    answers = [json.loads(row["raw_answers"]) for row in checkins]
    missing = {key: sum(answer.get(key) is None for answer in answers) for key in keys}
    validated = db.execute("""
        SELECT COUNT(*) FROM symptoms s JOIN checkins c USING (checkin_id)
        WHERE c.patient_id = ? AND s.validation_status = 'passed' AND s.source_type = 'questionnaire'
    """, (patient_id,)).fetchone()[0]
    return {
        "checkins": len(ids), "validated_observations": validated,
        "raw_answered_fields": len(ids) * len(keys) - sum(missing.values()), "missing_fields": missing,
        "unresolved_groups": sum(group["checkin_id"] in ids and group["review_status"] == "needs_review"
                                 for group in groups),
        "text_agreements": sum(group["checkin_id"] in ids and group["text_parser_status"] == "agreement"
                               for group in groups),
        "treatment_days": sorted({row["treatment_day"] for row in checkins if row["treatment_day"] is not None}),
    }
