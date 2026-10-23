"""Small, strict response contracts. No repair or extraction from model prose."""
from __future__ import annotations

import json
import math


class InvalidResponse(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponse("Duplicate JSON key")
        result[key] = value
    return result


def decode_json(value: str, max_chars: int = 16000) -> dict:
    if not isinstance(value, str) or len(value) > max_chars:
        raise InvalidResponse("Response exceeds allowed size")
    try:
        data = json.loads(value, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidResponse("Non-finite number")))
    except (ValueError, TypeError, RecursionError):
        raise InvalidResponse("Expected a single valid JSON object") from None
    if not isinstance(data, dict):
        raise InvalidResponse("Expected a JSON object")
    return data


def _keys(data: dict, expected: set[str]):
    if set(data) != expected:
        raise InvalidResponse("Unexpected or missing response fields")


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidResponse("Invalid text field")
    return value.strip()


def classification(value: str, max_chars: int = 16000) -> dict:
    data = decode_json(value, max_chars)
    _keys(data, {"relevant", "confidence", "category", "reason"})
    confidence = data["confidence"]
    if type(data["relevant"]) is not bool or type(confidence) not in (int, float):
        raise InvalidResponse("Invalid classification types")
    if not 0 <= confidence <= 1 or (isinstance(confidence, float) and not math.isfinite(confidence)):
        raise InvalidResponse("Confidence must be within 0..1")
    return {"relevant": data["relevant"], "confidence": confidence,
            "category": _text(data["category"], 120), "reason": _text(data["reason"], 1200)}


def analysis(value: str, max_chars: int = 16000) -> dict:
    data = decode_json(value, max_chars)
    _keys(data, {"type", "relevance", "summary", "connection", "impact", "action", "facts", "uncertainties"})
    if not isinstance(data["type"], str) or data["type"] not in {"risk", "opportunity", "neutral"}:
        raise InvalidResponse("Invalid analysis type")
    if type(data["relevance"]) is not int or not 0 <= data["relevance"] <= 100:
        raise InvalidResponse("Relevance must be an integer within 0..100")
    for key in ("summary", "connection", "impact", "action"):
        data[key] = _text(data[key], 2000)
    for key in ("facts", "uncertainties"):
        if not isinstance(data[key], list) or len(data[key]) > 12:
            raise InvalidResponse("Invalid evidence list")
        data[key] = [_text(item, 1000) for item in data[key]]
    return data


def answer(value: str, max_chars: int = 16000) -> dict:
    data = decode_json(value, max_chars)
    _keys(data, {"answer"})
    return {"answer": _text(data["answer"], 6000)}
