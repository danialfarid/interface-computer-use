from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)(?:\s*[=:]\s*)+)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]+"),
)
_SENSITIVE_FIELDS = re.compile(
    r"(?i)^(?:api[_-]?key|authorization|account(?:[_-]?number)?|email|member[_-]?id|name|password|phone|secret|ssn|token)$"
)
_PII_PATTERNS = (
    re.compile(r"(?i)\b(?:demo|synthetic)[_-]?(?:member|user)[_-]?\d+\b"),
    re.compile(r"\b\d{4,}\b"),
    re.compile(r"\$\s?[\d,]+(?:\.\d{2})?"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
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
