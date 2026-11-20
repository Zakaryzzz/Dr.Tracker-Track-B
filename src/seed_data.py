"""Generate, seed, verify, or reset the Phase 2 demo records."""
import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from random import Random
from uuid import uuid4

from .identifiers import normalize_identifiers
from .demo_medications import MEDICATION_NAME, MEDICATION_SOURCE, MEDICATION_SCHEDULES
from .database import DATABASE_PATH, TABLE_KEYS, apply_schema, connect, require

SEED = 732
EXPORT_PATH = DATABASE_PATH.with_name("synthetic_demo.json")
PROFILES = (
    ("Mara Vale", "1965-02-14", "Oropharyngeal cancer", 176, -0.32, 168),
    ("Felix Alder", "1971-07-09", "Laryngeal cancer", 192, -0.12, None),
    ("Nina Briar", "1959-11-21", "Oral cavity cancer", 153, 0.06, 148),
    ("Jonah Wren", "1968-04-25", "Nasopharyngeal cancer", 166, 0, None),
    ("Tessa Lark", "1976-08-18", "Hypopharyngeal cancer", 145, -0.06, 140),
)
CHEMO_DAYS = (1, 8, 15, 22)
CONFLICT_TEXT = "I threw up twice this afternoon, even though I marked zero earlier."
NEGATED_TEXT = "I haven't vomited today. My mouth feels dry."
EXTRA_REPORTS = {
    ("P001", 18): (3, "My throat was sore this morning, but the new pain is under my jaw."),
    ("P005", 6): (4, "Still thinking about that same dizzy spell by the sink this morning."),
    ("P005", 17): (4, "This evening I got dizzy again while folding laundry. The first spell was before breakfast."),
}


def daily_report(patient_id, day, rng):
    pain = rng.randint(2, 4)
    vomiting = 0
    text = rng.choice((
        "My mouth feels dry, especially when I wake up.",
        "The inside of my cheek is sore. Cold water feels easier to sip.",
        "I woke up tired, and food tasted different at lunch.",
        "Swallowing was uncomfortable this morning.",
    ))
    if patient_id == "P001":
        pain = min(9, 2 + day // 4 + rng.randint(0, 1))
        text = rng.choice((
            f"Swallowing at lunch made my throat hurt about {pain} out of 10.",
            f"The back of my throat is sore again, around a {pain} today.",
        ))
        if day == 25:
            text = "My throat hurts badly, and there's a sore inside my cheek."
    elif patient_id == "P002":
        if day in CHEMO_DAYS or day in (2, 16, 26):
            vomiting = rng.randint(1, 3)
            text = f"I vomited {vomiting} time(s) today and felt tired afterward."
            text = text.replace("1 time(s)", "once").replace(" time(s)", " times")
        elif day == 12:
            text = NEGATED_TEXT
    elif patient_id == "P003":
        pain = rng.randint(5, 7) if day < 13 else rng.randint(2, 4)
        if day == 13:
            text = "My care team adjusted my pain medication today. My mouth still feels sore."
        elif day == 4:
            text = "I started the medication my team mentioned. Swallowing is still uncomfortable."
    elif patient_id == "P004":
        pain = 2
        if day % 5 == 0:
            pain = None
            vomiting = None
            text = "I left a few questions blank. Mostly just tired today."

    answers = {
        "pain_score": pain,
        "pain_location": "back of throat" if pain is not None else None,
        "vomiting_count": vomiting,
        "fever": rng.choice((False, False, None)),
        "chills": False,
        "dizziness_score": rng.randint(0, 2),
        "mood_changes": None if day % 6 == 0 else False,
        "bleeding": False,
        "insomnia": rng.random() < 0.2,
        "cramps": False,
        "fatigue_score": rng.randint(2, 6),
        "dry_mouth_score": rng.randint(2, 7),
        "taste_changes": rng.random() < 0.4,
        "mouth_sores": True if "inside of my cheek" in text or "sore inside my cheek" in text else None,
    }
    if patient_id == "P005":
        special = {
            6: ("uncertain", "I think I felt dizzy by the sink this morning."),
            10: ("uncertain", "Earlier this week I felt off, maybe a little dizzy."),
            14: (None, CONFLICT_TEXT),
            17: (3, "I felt dizzy before breakfast today."),
            21: (0, NEGATED_TEXT),
        }
        if day in special:
            answers["dizziness_score"], text = special[day]
    return answers, text


def generate():
    rng = Random(SEED)
    data = {table: [] for table in ("patients", "checkins", "weight", "treatments")}
    for number, profile in enumerate(PROFILES, 1):
        name, birth, diagnosis, baseline, slope, threshold = profile
        patient_id = f"P{number:03d}"
        start = date(2026, 9, 7) + timedelta(days=number - 1)
        data["patients"].append(dict(
            patient_id=patient_id, patient_name=name, date_of_birth=birth,
            diagnosis=diagnosis, treatment_start_date=start.isoformat(),
            weight_threshold_lb=threshold,
        ))
        for offset in range(28):
            day = offset + 1
            current = start + timedelta(days=offset)
            answers, text = daily_report(patient_id, day, rng)
            reports = [("08:00:00", answers, text)]
            if (patient_id, day) in EXTRA_REPORTS:
                score, extra_text = EXTRA_REPORTS[patient_id, day]
                extra = dict.fromkeys(answers)
                if patient_id == "P001":
                    extra.update(pain_score=score, pain_location="under my jaw")
                else:
                    extra["dizziness_score"] = "uncertain" if day == 6 else score
                reports.append(("20:00:00", extra, extra_text))

            for index, (time, report, report_text) in enumerate(reports, 1):
                checkin_id = f"demo-{patient_id}-{day:02d}-{index}"
                data["checkins"].append(dict(
                    checkin_id=checkin_id, patient_id=patient_id,
                    submitted_at=f"{current.isoformat()}T{time}", treatment_day=day,
                    raw_answers=report, free_text=report_text, photo_reference=None,
                ))
                if offset % 3 == 0 or len(reports) > 1:
                    data["weight"].append(dict(
                        weight_id=f"{checkin_id}-weight", patient_id=patient_id,
                        checkin_id=checkin_id, measured_date=current.isoformat(),
                        weight_lb=round(baseline + slope * offset + rng.uniform(-0.8, 0.8), 1),
                    ))

        events = [
            (day, "radiation", "Radiation therapy", "completed", False)
            for day in range(1, 29) if (start + timedelta(days=day - 1)).weekday() < 5
        ]
        if patient_id in ("P002", "P005"):
            events.extend((day, "chemotherapy", "Chemotherapy", "completed", False)
                          for day in CHEMO_DAYS)
        if patient_id == "P003":
            events.extend([
                (4, "medication", "Pain medication", "started", True),
                (13, "medication", "Pain medication", "adjusted", True),
                (26, "medication", "Pain medication", "completed", False),
            ])
        events.append((28, "follow_up", "Treatment follow-up", "completed", False))
        for day, event_type, name, status, reported in events:
            frequency = source = None
            if patient_id == "P003" and event_type == "medication":
                name = MEDICATION_NAME
                status, frequency = MEDICATION_SCHEDULES[day]
                source = MEDICATION_SOURCE
            data["treatments"].append(dict(
                event_id=f"demo-{patient_id}-{day:02d}-{event_type}",
                patient_id=patient_id,
                checkin_id=f"demo-{patient_id}-{day:02d}-1" if reported else None,
                event_date=(start + timedelta(days=day - 1)).isoformat(),
                treatment_day=day, event_type=event_type, treatment_name=name,
                event_status=status, medication_frequency=frequency, medication_details_source=source,
            ))
    return normalize_identifiers(data)


def database_row(table, row):
    if table == "checkins":
        return {**row, "raw_answers": json.dumps(row["raw_answers"])}
    return row


def find_row(connection, table, row):
    key, _ = TABLE_KEYS[table]
    return connection.execute(
        f"SELECT * FROM {table} WHERE {key} = ?", (row[key],)
    ).fetchone()


def synchronize(connection, data, reset=False):
    tables = reversed(data) if reset else data
    for table in tables:
        key, _ = TABLE_KEYS[table]
        for source in data[table]:
            row = database_row(table, source)
            existing = find_row(connection, table, row)
            if existing is not None:
                if dict(existing) != row:
                    raise ValueError(f"Existing record differs: {table}.{row[key]}")
                if reset:
                    connection.execute(f"DELETE FROM {table} WHERE {key} = ?", (row[key],))
            elif not reset:
                columns = ", ".join(row)
                placeholders = ", ".join("?" for _ in row)
                connection.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    tuple(row.values()),
                )


def verify(connection, data):
    for table, rows in data.items():
        for source in rows:
            row = database_row(table, source)
            stored = find_row(connection, table, row)
            require(stored is not None and dict(stored) == row, f"Stored {table} row differs")
    require(len(data["patients"]) == 5, "Five demo patients")
    starts = {row["patient_id"]: date.fromisoformat(row["treatment_start_date"])
              for row in data["patients"]}
    days = Counter()
    answers = []
    texts = []
    for row in data["checkins"]:
        day = date.fromisoformat(row["submitted_at"][:10])
        patient = row["patient_id"]
        number = (day - starts[patient]).days + 1
        require(row["treatment_day"] == number, "Check-in treatment day")
        days[patient, number] += 1
        answers.extend(row["raw_answers"].values())
        texts.append(row["free_text"])
    for patient in starts:
        require(all(days[patient, day] for day in range(1, 29)), f"{patient}: daily coverage")
    for row in data["treatments"]:
        day = date.fromisoformat(row["event_date"])
        require(row["treatment_day"] == (day - starts[row["patient_id"]]).days + 1,
                "Event treatment day")
    require(any(count > 1 for count in days.values()), "Same-day check-ins")
    require(None in answers and "uncertain" in answers, "Missing and uncertain answers")
    require(NEGATED_TEXT in texts, "Negated report")
    require(any(row["free_text"] == CONFLICT_TEXT and row["raw_answers"]["vomiting_count"] == 0
                for row in data["checkins"]), "Questionnaire/text conflict")
    require(all(row["weight_lb"] > 0 for row in data["weight"]), "Positive weights")
    require(connection.execute("SELECT count(*) FROM checkins WHERE NOT json_valid(raw_answers)")
            .fetchone()[0] == 0, "Valid JSON")
    require(connection.execute("PRAGMA foreign_key_check").fetchone() is None, "Foreign keys")
    require(connection.execute("SELECT count(*) FROM symptoms").fetchone()[0] == 0,
            "Phase 2 expects an empty symptoms table")
    print("PASS: five demo patients, 28 dates each, same-day submissions, treatment days,")
    print("      JSON, positive weights, foreign keys, missing/uncertain answers,")
    print("      negation, questionnaire/text conflict, and empty symptoms.")


def print_counts(connection, data):
    print("Database totals:")
    for table in TABLE_KEYS:
        print(f"  {table}: {connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}")
    counts = {table: Counter(row["patient_id"] for row in rows) for table, rows in data.items()}
    for row in data["patients"]:
        patient = row["patient_id"]
        print(patient, ", ".join(f"{table}={counts[table][patient]}" for table in data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="Verify the existing demo records")
    mode.add_argument("--reset", action="store_true", help="Remove only matching demo records")
    args = parser.parse_args()
    data = generate()
    if args.verify or args.reset:
        if not DATABASE_PATH.is_file():
            parser.error("Initialize and seed the database first.")
    else:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with closing(connect()) as connection:
        connection.row_factory = sqlite3.Row
        if not (args.verify or args.reset):
            apply_schema(connection)
        with connection:
            connection.execute("BEGIN" if args.verify else "BEGIN IMMEDIATE")
            if not args.verify:
                synchronize(connection, data, reset=args.reset)
            if not args.reset:
                verify(connection, data)
                print_counts(connection, data)
        if args.reset:
            print("Matching demo rows removed. The reproducible JSON export is retained.")
        elif not args.verify:
            temporary_export = EXPORT_PATH.with_suffix(f".{uuid4().hex}.tmp")
            try:
                temporary_export.write_text(
                    json.dumps({"seed": SEED, **data}, indent=2) + "\n", encoding="utf-8"
                )
                temporary_export.replace(EXPORT_PATH)
            finally:
                temporary_export.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
