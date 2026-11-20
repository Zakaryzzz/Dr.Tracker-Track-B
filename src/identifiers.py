"""Numeric storage identifiers with compatibility for archived extraction IDs."""
import json
from functools import cache
from hashlib import sha256
from pathlib import Path

MAP_PATH = Path(__file__).resolve().parents[1] / "data" / "identifier_map.json"
KINDS = {"patient_id": "patients", "checkin_id": "checkins", "observation_id": "symptoms",
         "weight_id": "weight", "event_id": "treatments", "related_event_id": "treatments",
         "possible_duplicate_group": "duplicate_groups"}
LIST_KEYS = {"checkin_ids", "processed_checkin_ids", "failed_checkin_ids",
             "partial_checkin_ids", "possible_duplicate_links",
             "sample_failed_checkin_ids", "sample_partial_checkin_ids"}
INDEX_KEYS = {"errors", "raw_responses", "retry_responses", "initial_validation_errors",
              "initial_partial_candidates", "llm_revalidation_audit"}


@cache
def identifier_maps():
    return json.loads(MAP_PATH.read_text(encoding="utf-8")) if MAP_PATH.is_file() else {}


def identifier(kind, value):
    if value is None:
        return None
    if type(value) is int:
        if value <= 0 or value > 2**63 - 1:
            raise ValueError("Identifier must be a positive signed 64-bit integer")
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("Identifier must be an integer or a legacy identifier")
    if value.isdecimal():
        return identifier(kind, int(value))
    mapped = identifier_maps().get(kind, {}).get(value)
    if mapped is not None:
        return mapped
    # Namespace the hash so identifiers from different entity types remain independent.
    number = int.from_bytes(sha256(f"{kind}:{value}".encode()).digest()[:8], "big") & (2**63 - 1)
    return number or 1


def legacy_checkin(value):
    if type(value) is int:
        return next((old for old, new in identifier_maps().get("checkins", {}).items() if new == value), value)
    return value


def normalize_identifiers(value, context=None):
    """Normalize only identifier fields and indexes, leaving original evidence untouched."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            target = identifier("checkins", key) if context in INDEX_KEYS else key
            if key in KINDS:
                result[target] = identifier(KINDS[key], item)
            elif key in LIST_KEYS:
                result[target] = [identifier("checkins", entry) for entry in item]
            else:
                result[target] = normalize_identifiers(item, key)
        return result
    if isinstance(value, list):
        return [normalize_identifiers(item, context) for item in value]
    return value


def load_artifact(path):
    return normalize_identifiers(json.loads(path.read_text(encoding="utf-8")))
