"""Export independent questionnaire and keyword symptom candidates."""
import json
import re
from contextlib import closing

from .database import DATABASE_PATH, connect

SCORES = ("pain", "dizziness", "fatigue", "dry_mouth")
FLAGS = ("fever", "chills", "mood_changes", "bleeding", "insomnia", "cramps",
         "taste_changes", "mouth_sores")


def map_questionnaire(checkin_id, raw_answers):
    """Map explicitly answered questionnaire fields to observation candidates."""
    answers = json.loads(raw_answers)
    fields = {measure: f"{measure}_score" for measure in SCORES}
    fields.update({measure: measure for measure in FLAGS})
    fields["vomiting"] = "vomiting_count"
    observations = []
    for measure, field in fields.items():
        value = answers.get(field)
        if value is None:
            continue
        severity = count = None
        if value == "uncertain":
            status = "uncertain"
        elif measure in FLAGS and type(value) is bool:
            status = "present" if value else "absent"
        elif type(value) is int and value >= 0 and (
            measure == "vomiting" or (measure in SCORES and value <= 10)
        ):
            status = "present" if value else "absent"
            if measure in SCORES:
                severity = value
            else:
                count = value
        else:
            raise ValueError(f"{checkin_id}: invalid questionnaire field {field}")

        location = answers.get("pain_location") if measure == "pain" else None
        observations.append(dict(
            checkin_id=checkin_id, measure=measure, status=status,
            severity_score=severity, frequency_count=count, pain_location=location,
            source_type="questionnaire", extraction_method="questionnaire",
        ))
    return observations


PATTERNS = {
    "pain": (
        r"\b(?:(?P<location>back of (?:my |the )?throat|throat|jaw|neck|tongue)"
        r"(?:\s+(?:is|was|feels?|felt))?\s+(?:hurts?|hurt|sore|aching|pain)|"
        r"swallowing (?:is |was )?(?:still )?(?:painful|uncomfortable)|"
        r"pain(?!\s+(?:medication|medicine|meds|killers?|relief|management|team)\b)|"
        r"aches?|hurts?)\b"
    ),
    "vomiting": r"\b(?:vomit(?:s|ed|ing)?|throw(?:s|ing)? up|threw up|puk(?:e[ds]?|ing))\b",
    "dizziness": r"\b(?:dizz(?:y|iness)|light[- ]headed(?:ness)?|room (?:was |is )?spinning)\b",
    "fatigue": r"\b(?:fatigu(?:e|ed)|tired(?:ness)?|exhaust(?:ed|ion)|worn out)\b",
    "dry_mouth": r"\b(?:dry mouth|mouth (?:is |was |feels? |felt )?(?:very )?dry)\b",
    "mouth_sores": (
        r"\b(?:mouth (?:sores?|ulcers?)|canker sores?|"
        r"(?:sores?|ulcers?) (?:inside|in) (?:my |the )?(?:mouth|cheek)|"
        r"inside of (?:my |the )?cheek (?:is|was|feels?) sore)\b"
    ),
    "fever": r"\b(?:fevers?|feverish|high temperature)\b",
    "chills": r"\b(?:chills?|shiver(?:s|ing|ed)?)\b",
    "bleeding": r"\b(?:bleed(?:s|ing)?|bled|bloody saliva)\b",
    "insomnia": r"\b(?:insomnia|(?:trouble|difficulty) sleeping|couldn['’]t sleep)\b",
    "cramps": r"\b(?:cramps?|cramping|muscle spasms?)\b",
    "mood_changes": r"\b(?:mood (?:changes?|swings?)|irritable|low mood)\b",
    "taste_changes": (
        r"\b(?:taste changes?|lost (?:my |the )?sense of taste|"
        r"(?:tastes?|tasted) (?:different|metallic|off)|metallic taste)\b"
    ),
}
SYMPTOM = re.compile(
    "|".join(f"(?P<{measure}>{pattern})" for measure, pattern in PATTERNS.items()),
    re.IGNORECASE,
)
CLAUSE = re.compile(
    r"(?<=[.!?;])\s+|\s*,?\s+\b(?:but|however|even though|although)\b\s+"
    r"|\s*,?\s+\band\s+(?=(?:I|my|felt|had|there['’]s)\b)",
    re.IGNORECASE,
)
NEGATION = re.compile(
    r"\b(?:no|not(?!\s+(?:sure|only)\b)|never|without|den(?:y|ies|ied)|"
    r"(?:haven|hasn|hadn|didn|don|doesn|wasn|weren|isn|aren)['’]t)\b", re.I
)
UNCERTAINTY = re.compile(
    r"\b(?:maybe|perhaps|possibly|possible|might|could be|i think|i wonder|"
    r"not sure|unsure|uncertain|felt like)\b", re.I
)
CONDITIONAL = re.compile(r"\b(?:if|in case|watch for|risk of|worried about)\b", re.I)
SEVERITY = re.compile(
    r"^\s*(?:was |is |at |about |rated |: ?)*(10|[0-9])\s*(?:/\s*10|out of 10)\b", re.I
)
COUNT = re.compile(r"^\s*(?:(once|twice|thrice)\b|(\d+|zero|one|two|three|four|five)\s+times?\b)", re.I)
NUMBER_WORDS = dict(zip(("zero", "one", "two", "three", "four", "five"), range(6)))
NUMBER_WORDS.update(once=1, twice=2, thrice=3)
TIME = re.compile(
    r"\b(?:earlier this week|last night|this (?:morning|afternoon|evening)|"
    r"before breakfast|after (?:breakfast|lunch|dinner)|at lunch|yesterday|today|tonight)\b", re.I
)
PAIN_LOCATION = re.compile(
    r"^\s*(?:is |was )?(?:in|under|around|behind|on)\s+"
    r"(?:my |the )?(?:left |right )?(?:jaw|throat|neck|tongue|cheek|mouth|gums|head)\b", re.I
)


def parse_keywords(checkin_id, free_text):
    """Extract explicit keyword mentions with local context and original wording."""
    observations = []
    for clause in CLAUSE.split(free_text or ""):
        if clause.rstrip().endswith("?"):
            continue
        matches = list(SYMPTOM.finditer(clause))
        previous_end = 0
        previous_status = None
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
            prefix = clause[previous_end:match.start()]
            suffix = clause[match.end():end]
            previous_end = match.end()
            if CONDITIONAL.search(prefix):
                continue

            measure = match.lastgroup
            score_match = SEVERITY.match(suffix)
            severity = int(score_match[1]) if score_match else None
            count = None
            if measure == "vomiting" and (count_match := COUNT.match(suffix)):
                token = (count_match[1] or count_match[2]).lower()
                count = int(token) if token.isdigit() else NUMBER_WORDS[token]

            if UNCERTAINTY.search(prefix) or re.match(r"\s*,?\s*(?:maybe|perhaps)\b", suffix, re.I):
                status = "uncertain"
            elif (
                NEGATION.search(prefix) or severity == 0 or count == 0
                or (previous_status == "absent"
                    and re.fullmatch(r"[\s,]*(?:(?:or|nor|and)\s+)?", prefix, re.I))
            ):
                status = "absent"
            else:
                status = "present"
            previous_status = status

            location = match["location"] if measure == "pain" else None
            if measure == "pain" and not location and (site := PAIN_LOCATION.match(suffix)):
                location = site[0].strip()
            timing = TIME.search(suffix) or (TIME.search(prefix) if index == 0 else None)
            observations.append(dict(
                checkin_id=checkin_id, measure=measure, status=status,
                severity_score=severity, frequency_count=count,
                observed_time_text=timing[0] if timing else None,
                pain_location=location, original_phrase=clause.strip(" ,"),
                source_type="free_text", extraction_method="keyword",
            ))
    return observations


def main():
    questionnaire = []
    keywords = []
    # SQLite's read-only URI also prevents accidental database creation.
    with closing(connect(DATABASE_PATH.as_uri() + "?mode=ro", uri=True)) as connection:
        for checkin_id, raw_answers, free_text in connection.execute(
            "SELECT checkin_id, raw_answers, free_text FROM checkins ORDER BY checkin_id"
        ):
            questionnaire.extend(map_questionnaire(checkin_id, raw_answers))
            keywords.extend(parse_keywords(checkin_id, free_text))

    for name, observations in (("questionnaire", questionnaire), ("keyword", keywords)):
        path = DATABASE_PATH.with_name(f"{name}_extractions.json")
        path.write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{name}: {len(observations)} candidates -> {path.name}")


if __name__ == "__main__":
    main()
