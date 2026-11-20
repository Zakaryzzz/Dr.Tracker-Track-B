BEGIN;

CREATE TABLE IF NOT EXISTS patients (
    patient_id INTEGER PRIMARY KEY NOT NULL CHECK (patient_id IS NULL OR (typeof(patient_id) = 'integer' AND patient_id > 0)),
    patient_name TEXT NOT NULL CHECK (patient_name IS NULL OR typeof(patient_name) = 'text'),
    date_of_birth DATE NOT NULL CHECK ((date_of_birth IS NULL OR typeof(date_of_birth) = 'text') AND date(date_of_birth, '+0 days') IS date_of_birth AND (date_of_birth IS NULL OR length(date_of_birth) = 10)),
    diagnosis TEXT NOT NULL CHECK (diagnosis IS NULL OR typeof(diagnosis) = 'text'),
    treatment_start_date DATE NOT NULL CHECK ((treatment_start_date IS NULL OR typeof(treatment_start_date) = 'text') AND date(treatment_start_date, '+0 days') IS treatment_start_date AND (treatment_start_date IS NULL OR length(treatment_start_date) = 10)),
    weight_threshold_lb REAL CHECK (weight_threshold_lb IS NULL OR typeof(weight_threshold_lb) IN ('real', 'integer'))
);

CREATE TABLE IF NOT EXISTS checkins (
    checkin_id INTEGER PRIMARY KEY NOT NULL CHECK (checkin_id IS NULL OR (typeof(checkin_id) = 'integer' AND checkin_id > 0)),
    patient_id INTEGER NOT NULL REFERENCES patients(patient_id) CHECK (patient_id IS NULL OR (typeof(patient_id) = 'integer' AND patient_id > 0)),
    submitted_at DATETIME NOT NULL CHECK (
        typeof(submitted_at) = 'text' AND length(submitted_at) = 19
        AND strftime('%Y-%m-%dT%H:%M:%S', submitted_at, '+0 seconds') IS submitted_at
    ),
    treatment_day INTEGER CHECK (treatment_day IS NULL OR (typeof(treatment_day) = 'integer' AND treatment_day >= 1)),
    raw_answers TEXT NOT NULL CHECK (json_valid(raw_answers)) CHECK (raw_answers IS NULL OR typeof(raw_answers) = 'text'),
    free_text TEXT CHECK (free_text IS NULL OR typeof(free_text) = 'text'),
    photo_reference TEXT CHECK (photo_reference IS NULL OR typeof(photo_reference) = 'text'),
    UNIQUE (checkin_id, patient_id)
);

CREATE TABLE IF NOT EXISTS treatments (
    event_id INTEGER PRIMARY KEY NOT NULL CHECK (event_id IS NULL OR (typeof(event_id) = 'integer' AND event_id > 0)),
    patient_id INTEGER NOT NULL REFERENCES patients(patient_id) CHECK (patient_id IS NULL OR (typeof(patient_id) = 'integer' AND patient_id > 0)),
    checkin_id INTEGER CHECK (checkin_id IS NULL OR (typeof(checkin_id) = 'integer' AND checkin_id > 0)),
    event_date DATE NOT NULL CHECK ((event_date IS NULL OR typeof(event_date) = 'text') AND date(event_date, '+0 days') IS event_date AND (event_date IS NULL OR length(event_date) = 10)),
    treatment_day INTEGER CHECK (treatment_day IS NULL OR (typeof(treatment_day) = 'integer' AND treatment_day >= 1)),
    event_type TEXT NOT NULL CHECK (event_type IS NULL OR typeof(event_type) = 'text'),
    treatment_name TEXT NOT NULL CHECK (treatment_name IS NULL OR typeof(treatment_name) = 'text'),
    event_status TEXT NOT NULL CHECK (event_status IS NULL OR typeof(event_status) = 'text'),
    FOREIGN KEY (checkin_id, patient_id) REFERENCES checkins(checkin_id, patient_id)
);

CREATE TABLE IF NOT EXISTS symptoms (
    observation_id INTEGER PRIMARY KEY NOT NULL CHECK (observation_id IS NULL OR (typeof(observation_id) = 'integer' AND observation_id > 0)),
    checkin_id INTEGER NOT NULL REFERENCES checkins(checkin_id) CHECK (checkin_id IS NULL OR (typeof(checkin_id) = 'integer' AND checkin_id > 0)),
    measure TEXT NOT NULL CHECK (measure IS NULL OR typeof(measure) = 'text'),
    status TEXT NOT NULL CHECK (status IN ('present', 'absent', 'uncertain')) CHECK (status IS NULL OR typeof(status) = 'text'),
    severity_score INTEGER CHECK (severity_score IS NULL OR (typeof(severity_score) = 'integer' AND severity_score BETWEEN 0 AND 10)),
    frequency_count INTEGER CHECK (frequency_count IS NULL OR (typeof(frequency_count) = 'integer' AND frequency_count >= 0)),
    frequency_period TEXT CHECK (frequency_period IS NULL OR typeof(frequency_period) = 'text'),
    value_text TEXT CHECK (value_text IS NULL OR typeof(value_text) = 'text'),
    original_phrase TEXT CHECK (original_phrase IS NULL OR typeof(original_phrase) = 'text'),
    observed_date DATE CHECK ((observed_date IS NULL OR typeof(observed_date) = 'text') AND date(observed_date, '+0 days') IS observed_date AND (observed_date IS NULL OR length(observed_date) = 10)),
    observed_time_text TEXT CHECK (observed_time_text IS NULL OR typeof(observed_time_text) = 'text'),
    source_type TEXT NOT NULL CHECK (source_type IN ('questionnaire', 'free_text')) CHECK (source_type IS NULL OR typeof(source_type) = 'text'),
    pain_location TEXT CHECK (pain_location IS NULL OR typeof(pain_location) = 'text'),
    related_event_id INTEGER REFERENCES treatments(event_id) CHECK (related_event_id IS NULL OR (typeof(related_event_id) = 'integer' AND related_event_id > 0)),
    extraction_method TEXT NOT NULL CHECK (
        extraction_method IN ('questionnaire', 'keyword', 'llm', 'manual', 'reconciled')
    ),
    possible_duplicate_group INTEGER CHECK (possible_duplicate_group IS NULL OR (typeof(possible_duplicate_group) = 'integer' AND possible_duplicate_group > 0)),
    review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'needs_review', 'reviewed')) CHECK (review_status IS NULL OR typeof(review_status) = 'text'),
    review_reason TEXT CHECK (review_reason IS NULL OR typeof(review_reason) = 'text'),
    validation_status TEXT NOT NULL CHECK (validation_status IN ('pending', 'passed', 'failed')) CHECK (validation_status IS NULL OR typeof(validation_status) = 'text')
);

CREATE TABLE IF NOT EXISTS weight (
    weight_id INTEGER PRIMARY KEY NOT NULL CHECK (weight_id IS NULL OR (typeof(weight_id) = 'integer' AND weight_id > 0)),
    patient_id INTEGER NOT NULL REFERENCES patients(patient_id) CHECK (patient_id IS NULL OR (typeof(patient_id) = 'integer' AND patient_id > 0)),
    checkin_id INTEGER NOT NULL CHECK (checkin_id IS NULL OR (typeof(checkin_id) = 'integer' AND checkin_id > 0)),
    measured_date DATE NOT NULL CHECK ((measured_date IS NULL OR typeof(measured_date) = 'text') AND date(measured_date, '+0 days') IS measured_date AND (measured_date IS NULL OR length(measured_date) = 10)),
    weight_lb REAL NOT NULL CHECK (typeof(weight_lb) IN ('real', 'integer') AND weight_lb > 0),
    FOREIGN KEY (checkin_id, patient_id) REFERENCES checkins(checkin_id, patient_id)
);

COMMIT;
