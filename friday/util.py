"""Small shared helpers."""
from __future__ import annotations

import hashlib
import json
import re

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 [REDACTED]"),
    (re.compile(r"\bfri_[A-Za-z0-9_\-]{20,}"), "fri_[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "sk-[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{16,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b(\s*[=:]\s*)(\S+)"), r"\1\2[REDACTED]"),
]


def redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def fingerprint(tool: str, args: dict) -> str:
    return hashlib.sha256(f"{tool}\n{canonical_json(args)}".encode()).hexdigest()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"
