from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


_SECRET_PATTERNS = (
    re.compile(r"(?i)(https?://)[^\s/@:]+(?::[^\s/@]*)?@"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)(?:\s*[=:]\s*)+)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]+"),
)
_SENSITIVE_FIELDS = re.compile(
    r"(?i)^(?:api[_-]?key|authorization|account(?:[_-]?number)?|balance|current[_-]?savings[_-]?balance|email|member[_-]?id|name|password|phone|secret|ssn|token)$"
)
_PII_PATTERNS = (
    re.compile(r"(?i)\b(?:demo|synthetic)[_-]?(?:member|user)[_-]?\d+\b"),
    re.compile(r"\b\d{4,}\b"),
    re.compile(r"\$\s?[\d,]+(?:\.\d{2})?"),
    re.compile(r"\b\d[\d,]*\.\d{2}\b"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
    re.compile(r"(?i)\b\d{1,5}\s+[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){1,2}\b"),
    re.compile(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b"),
    re.compile(r"\b\d{1,5}\s+[A-Z]{2,}(?:\s+[A-Z]{2,}){1,2}\b"),
    re.compile(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,})+\b"),
    re.compile(r"(?im)\b(?:member\s+)?(?:name|address)\s*:\s*[^\r\n]+"),
)


def redact_text(value: str) -> str:
    """Remove credentials and identifier-like values before evidence is persisted."""

    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda match: f"{match.group(1)}<REDACTED>", result)
    for pattern in _PII_PATTERNS:
        result = pattern.sub("<REDACTED>", result)
    return result


def redact_value(name: str, value: Any) -> Any:
    if value is None:
        return None
    if _SENSITIVE_FIELDS.match(name):
        return "<REDACTED>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_value(str(key), item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(name, item) for item in value]
    return value


def redact_url(value: str) -> str:
    """Preserve a target route while removing sensitive query values."""

    parsed = urlsplit(value)
    sensitive = _SENSITIVE_FIELDS
    query = [
        (key, "<REDACTED>" if sensitive.match(key) else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    encoded_query = "&".join(
        f"{quote(key, safe='')}={quote(item, safe='{}')}" for key, item in query
    )
    return urlunsplit((parsed.scheme, safe_netloc, parsed.path, encoded_query, ""))


def redact_artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact the small set of artifact fields that can carry runtime values."""

    result = dict(payload)
    target = dict(result.get("target", {}))
    if isinstance(target.get("url"), str):
        target["url"] = redact_url(target["url"])
    result["target"] = target
    steps = []
    for raw_step in result.get("steps", []):
        step = dict(raw_step)
        if isinstance(step.get("description"), str):
            step["description"] = redact_text(step["description"])
        steps.append(step)
    result["steps"] = steps
    return result
