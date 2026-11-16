BEGIN;

CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY NOT NULL,
    patient_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL CHECK (date(date_of_birth, '+0 days') IS date_of_birth AND (date_of_birth IS NULL OR length(date_of_birth) = 10)),
    diagnosis TEXT NOT NULL,
    treatment_start_date TEXT NOT NULL CHECK (date(treatment_start_date, '+0 days') IS treatment_start_date AND (treatment_start_date IS NULL OR length(treatment_start_date) = 10)),
    weight_threshold_lb REAL
) STRICT;

CREATE TABLE IF NOT EXISTS checkins (
    checkin_id TEXT PRIMARY KEY NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    submitted_at TEXT NOT NULL CHECK (
        substr(submitted_at, 11, 1) = 'T'
        AND datetime(submitted_at) IS NOT NULL
    ),
    treatment_day INTEGER CHECK (treatment_day >= 1),
    raw_answers TEXT NOT NULL CHECK (json_valid(raw_answers)),
    free_text TEXT,
    photo_reference TEXT,
    UNIQUE (checkin_id, patient_id)
) STRICT;

CREATE TABLE IF NOT EXISTS treatments (
    event_id TEXT PRIMARY KEY NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    checkin_id TEXT,
    event_date TEXT NOT NULL CHECK (date(event_date, '+0 days') IS event_date AND (event_date IS NULL OR length(event_date) = 10)),
    treatment_day INTEGER CHECK (treatment_day >= 1),
    event_type TEXT NOT NULL,
    treatment_name TEXT NOT NULL,
    event_status TEXT NOT NULL,
    FOREIGN KEY (checkin_id, patient_id) REFERENCES checkins(checkin_id, patient_id)
) STRICT;

CREATE TABLE IF NOT EXISTS symptoms (
    observation_id TEXT PRIMARY KEY NOT NULL,
    checkin_id TEXT NOT NULL REFERENCES checkins(checkin_id),
    measure TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('present', 'absent', 'uncertain')),
    severity_score INTEGER CHECK (severity_score BETWEEN 0 AND 10),
    frequency_count INTEGER CHECK (frequency_count >= 0),
    frequency_period TEXT,
    value_text TEXT,
    original_phrase TEXT,
    observed_date TEXT CHECK (date(observed_date, '+0 days') IS observed_date AND (observed_date IS NULL OR length(observed_date) = 10)),
    observed_time_text TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('questionnaire', 'free_text')),
    pain_location TEXT,
    related_event_id TEXT REFERENCES treatments(event_id),
    extraction_method TEXT NOT NULL CHECK (
        extraction_method IN ('questionnaire', 'keyword', 'llm', 'manual', 'reconciled')
    ),
    possible_duplicate_group TEXT,
    review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'needs_review', 'reviewed')),
    review_reason TEXT,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('pending', 'passed', 'failed'))
) STRICT;

CREATE TABLE IF NOT EXISTS weight (
    weight_id TEXT PRIMARY KEY NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    checkin_id TEXT NOT NULL,
    measured_date TEXT NOT NULL CHECK (date(measured_date, '+0 days') IS measured_date AND (measured_date IS NULL OR length(measured_date) = 10)),
    weight_lb REAL NOT NULL CHECK (weight_lb > 0),
    FOREIGN KEY (checkin_id, patient_id) REFERENCES checkins(checkin_id, patient_id)
) STRICT;

COMMIT;
