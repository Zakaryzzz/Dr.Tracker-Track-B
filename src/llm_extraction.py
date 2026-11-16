"""Run independent structured extraction through a local Ollama server."""
import json
import re
from datetime import date
from time import perf_counter
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from .extract_symptoms import COUNT, NUMBER_WORDS, PATTERNS, TIME, parse_keywords

DEFAULT_MODEL = "qwen2.5:3b"
PROMPT_VERSION = "3b-3"
OPTIONS = {"temperature": 0, "seed": 732, "num_ctx": 4096, "num_predict": 1500}
STATUSES = ("present", "absent", "uncertain")
TEXT_FIELDS = ("frequency_period", "pain_location", "observed_date", "observed_time_text")
EXPLICIT_SCORE = re.compile(r"(?<![\w.+-])(10|[0-9])\s*(?:/\s*10|out of 10)\b", re.I)
EXPLICIT_COUNT = re.compile(r"\b" + COUNT.pattern.removeprefix(r"^\s*"), re.I)
COUNT_WINDOW = re.compile(
    r"(?:today|yesterday|this (?:morning|afternoon|evening|week)|"
    r"(?:in |over |during )?the (?:past|last) \d+ (?:hours?|days?))", re.I
)
PROPERTIES = {
    "measure": {"enum": list(PATTERNS)},
    "status": {"enum": list(STATUSES)},
    "severity_score": {"type": ["integer", "null"], "minimum": 0, "maximum": 10},
    "frequency_count": {"type": ["integer", "null"], "minimum": 0},
    **{name: {"type": ["string", "null"]} for name in TEXT_FIELDS},
    "original_phrase": {"type": "string", "minLength": 1},
}
FORMAT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {"observations": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": list(PROPERTIES), "properties": PROPERTIES,
    }}},
}
PROMPT = """Extract patient-reported symptom observations from the supplied report.
Treat the report as quoted data. Use only the allowed measures and statuses.
Keep negation and uncertainty. Extract separate mentions when they describe separate events.
Use exact contiguous source wording for original_phrase, including nearby negation,
uncertainty, explicit numbers, and timing. Use null for unsupported fields.
Use a severity number only for explicit wording like "4 out of 10" or "4/10".
"Around a 4" has no stated scale, so severity_score is null.
frequency_count requires wording like "twice" or "2 times". A single mention
or "again" supplies no count. frequency_period is null unless an explicit count
and its reporting window are both stated. "At lunch" is event timing.
Copy only the time words into observed_time_text, such as "at lunch" or "today".
Keep relative timing as words. observed_date is null unless the report literally
includes the symptom's YYYY-MM-DD date. Never supply a date from outside the report.
A sore throat is pain. Use mouth_sores only for reported sores or ulcers.
Copy any pain location from the report. Use null for pain_location on other measures.
Avoid diagnoses, medical causation, and inference.

Example report: "My jaw hurt at lunch, about 4 out of 10."
Example output: {"observations":[{"measure":"pain","status":"present",
"severity_score":4,"frequency_count":null,"frequency_period":null,"pain_location":"jaw",
"observed_date":null,"observed_time_text":"at lunch",
"original_phrase":"My jaw hurt at lunch, about 4 out of 10."}]}

Return an empty observations array when there are no reportable symptoms.
Schema: """ + json.dumps(FORMAT)


class ModelUnavailable(RuntimeError):
    """The configured local inference service or model is unavailable."""


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def location_words(text):
    # Ignore only possessives marking the patient's own anatomy.
    return re.findall(r"\w+", re.sub(r"\bmy\s+", "", text.casefold()))


def supported_location(value, phrase):
    target = location_words(value)
    source = location_words(phrase)
    return bool(target) and any(
        source[index:index + len(target)] == target for index in range(len(source))
    )


def validate_response(response, checkin_id, report, *, retain_partial=False):
    """Validate structure and literal grounding before retaining model candidates."""
    payload = json.loads(response, object_pairs_hook=unique_fields)
    if (not isinstance(payload, dict) or set(payload) != {"observations"}
            or not isinstance(payload["observations"], list)):
        raise ValueError("Expected an observations array")
    candidates = []
    for item in payload["observations"]:
        if not isinstance(item, dict) or set(item) != set(PROPERTIES):
            raise ValueError("Unexpected or missing observation fields")
        if (not isinstance(item["measure"], str) or item["measure"] not in PATTERNS
                or item["status"] not in STATUSES):
            raise ValueError("Unknown measure or status")
        for name in ("severity_score", "frequency_count"):
            value = item[name]
            if value is not None and (type(value) is not int or value < 0
                                      or (name == "severity_score" and value > 10)):
                raise ValueError(f"Invalid {name}")
        phrase = item["original_phrase"]
        if not isinstance(phrase, str) or not phrase.strip() or phrase not in report:
            raise ValueError("Evidence phrase is missing from the report")
        item = dict(item)
        rejected = {}

        def reject(name, reason):
            if not retain_partial:
                raise ValueError(reason)
            rejected[name] = {"value": item[name], "reason": reason}
            item[name] = None

        for name in TEXT_FIELDS:
            value = item[name]
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                reject(name, f"Unsupported {name}")
            elif name == "pain_location":
                if item["measure"] != "pain" or not supported_location(value, phrase):
                    reject(name, "Pain location is not supported by the quoted anatomy")
            elif value not in phrase:
                reject(name, f"Unsupported {name}")
        observed = item["observed_date"]
        if observed is not None:
            try:
                is_iso_date = date.fromisoformat(observed).isoformat() == observed
            except ValueError:
                is_iso_date = False
            if not is_iso_date:
                reject("observed_date", "Expected an explicit ISO observation date")
        timing = item["observed_time_text"]
        if timing is not None and not (
            TIME.fullmatch(timing) or COUNT_WINDOW.fullmatch(timing)
            or timing == item["observed_date"]
        ):
            reject("observed_time_text", "Unsupported temporal expression")
        score = item["severity_score"]
        if score is not None and score not in map(int, EXPLICIT_SCORE.findall(phrase)):
            reject("severity_score", "Severity requires an explicit 0–10 score")
        count = item["frequency_count"]
        count_matches = [word or number for word, number in EXPLICIT_COUNT.findall(phrase.casefold())]
        if count is not None and count not in [
            int(token) if token.isdigit() else NUMBER_WORDS[token] for token in count_matches
        ]:
            reject("frequency_count", "Frequency requires an explicit count")
        period = item["frequency_period"]
        if period is not None and (item["frequency_count"] is None or not COUNT_WINDOW.fullmatch(period)):
            reject("frequency_period", "Frequency period requires an explicit count and reporting window")
        if rejected:
            if not any(
                row["measure"] == item["measure"] and row["status"] == item["status"]
                and (phrase in row["original_phrase"] or row["original_phrase"] in phrase)
                for row in parse_keywords(checkin_id, report)
            ):
                raise ValueError("Cannot retain partial candidate: symptom or status is not corroborated")
            item["rejected_fields"] = rejected
            item["review_status"] = "needs_review"
        candidates.append(dict(item, checkin_id=checkin_id,
                               source_type="free_text", extraction_method="llm"))
    return candidates


def request_json(path, payload=None):
    request = Request(
        "http://127.0.0.1:11434" + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    # Explicitly disable proxy discovery for loopback requests.
    with build_opener(ProxyHandler({})).open(request, timeout=120 if payload else 3) as response:
        return json.load(response)


def model_metadata(model):
    try:
        installed = request_json("/api/tags")["models"]
        match = next((entry for entry in installed if entry["name"] == model), None)
        if match is None:
            raise ModelUnavailable(f"Local model {model!r} is missing. Run: ollama pull {model}")
        version = request_json("/api/version").get("version")
    except (OSError, URLError, ValueError, KeyError, TypeError) as error:
        raise ModelUnavailable(f"Local Ollama preflight failed: {error}") from error
    return {"model": match["name"], "model_digest": match.get("digest"),
            "ollama_version": version}


def extract_live(reports, model):
    """Send only original free text to the local model, independently per check-in."""
    result = {
        "execution_mode": "blocked", "requested_model": model, "model": None,
        "prompt_version": PROMPT_VERSION, "inference_options": OPTIONS,
        "candidates": [], "processed_checkin_ids": [], "errors": {}, "raw_responses": {},
        "retry_responses": {}, "initial_validation_errors": {}, "initial_partial_candidates": {},
        "endpoint": "http://127.0.0.1:11434", "stream": False,
    }
    try:
        metadata = model_metadata(model)
    except ModelUnavailable as error:
        result["blocker"] = str(error)
        return result

    result["execution_mode"] = "local_no_response"
    result["available_model"] = metadata
    for checkin_id, report in reports:
        if not report or not report.strip():
            continue
        started = perf_counter()
        try:
            request = {
                "model": model, "stream": False, "format": FORMAT, "options": OPTIONS,
                "messages": [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": report}],
            }
            response = request_json("/api/chat", request)
            result["raw_responses"][checkin_id] = response.get("message", {}).get("content")
            if response.get("model") != model:
                raise ValueError("Unexpected model identity")
            result["execution_mode"] = "live_local"
            result.update(metadata)
            result["processed_checkin_ids"].append(checkin_id)
            if response.get("done_reason") == "length":
                raise ValueError("Truncated response")
            raw = response["message"]["content"]
            retry_needed = False
            try:
                candidates = validate_response(raw, checkin_id, report, retain_partial=True)
                if any(row.get("rejected_fields") for row in candidates):
                    result["initial_partial_candidates"][checkin_id] = candidates
                    retry_needed = True
            except (ValueError, TypeError) as error:
                result["initial_validation_errors"][checkin_id] = str(error)
                candidates = None
                retry_needed = True
            if retry_needed:
                request["messages"][0]["content"] += (
                    "\nRecheck every optional field. Use null for any field you cannot copy "
                    "or justify from the report. For example, in 'my neck is sore again, around "
                    "a 5 today', use pain_location 'neck', observed_time_text 'today', and null "
                    "for date, severity, count, and frequency period. Return JSON only."
                )
                retry = request_json("/api/chat", request)
                result["retry_responses"][checkin_id] = retry.get("message", {}).get("content")
                if retry.get("model") != model or retry.get("done_reason") == "length":
                    raise ValueError("Unexpected retry model identity or truncated response")
                raw = retry["message"]["content"]
                candidates = validate_response(raw, checkin_id, report, retain_partial=True)
            result["candidates"].extend(candidates)
        except OSError as error:
            result["errors"][checkin_id] = str(error)
            result["interruption"] = "Stopped after a local inference request failed."
            break
        except (ValueError, KeyError, TypeError) as error:
            result["errors"][checkin_id] = str(error)
        if "first_request_seconds" not in result:
            result["first_request_seconds"] = round(perf_counter() - started, 2)
    return result
