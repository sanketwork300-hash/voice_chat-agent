"""Remove secrets from text and data before they reach the customer, the model, or a log."""

import re
from typing import Any

REDACTED = "[redacted]"

_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b"),                      # AWS key and principal IDs
    re.compile(r"(?i)\b(aws_secret_access_key|secret_access_key|secretaccesskey)\b(\s*[=:]\s*)\S+"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)\b(\s*[=:]\s*)\S+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{16,}=*"),
    re.compile(r"\b([a-z][a-z0-9+.-]*://[^\s:/@]+):[^\s@/]+@"),                  # credentials in a URL
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]

_SENSITIVE_KEYS = re.compile(r"(?i)(secret|password|passwd|token|api[_-]?key|credential)")


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: f"{m.group(1)}:{REDACTED}@", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_data(value: Any) -> Any:
    """Redact strings inside a JSON-like structure; values under sensitive keys are dropped entirely."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_data(v) for v in value]
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SENSITIVE_KEYS.search(k) and isinstance(v, (str, int, float))
            else redact_data(v)
            for k, v in value.items()
        }
    return value


class StreamRedactor:
    """Redact streamed text.

    A secret can be split across fragments, so text is released only up to the
    last whitespace; the unfinished tail is held until more arrives.
    """

    def __init__(self) -> None:
        self._held = ""

    def feed(self, fragment: str) -> str:
        self._held += fragment
        cut = max(self._held.rfind(" "), self._held.rfind("\n"))
        if cut < 0:
            return ""
        ready, self._held = self._held[:cut + 1], self._held[cut + 1:]
        return redact(ready)

    def flush(self) -> str:
        ready, self._held = self._held, ""
        return redact(ready)
