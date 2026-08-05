"""
Lightweight, regex-based PII/secret redaction.

The copilot's read tools return real cluster data — pod logs, Kubernetes
event messages, RCA reports — straight from the OpenTelemetry Demo (a live
e-commerce app with checkout/cart flows) to a third-party LLM API
(Anthropic/Groq), and that data then gets persisted indefinitely in
`IncidentStore`'s SQLite file. Nothing upstream guarantees those logs never
contain a customer email, phone number, or a stray secret a service happened
to log. This module is a best-effort scrub applied at the boundary where
that data leaves the platform's own tools, not a compliance-grade PII
classifier.

Deliberately NOT redacted: IP addresses and hostnames. Those are core
operational signal for an SRE tool (pod IPs, service IPs) and, on their own,
aren't personal data — redacting them would make the tools measurably less
useful for a marginal privacy gain.

Scope/limitations: pattern-based, so it will miss anything that doesn't look
like the patterns below (e.g. a name in free text) and can occasionally
false-positive on something that merely looks like one of these shapes.
Treat this as a floor, not a guarantee.
"""

import re
from typing import Any


_PATTERNS = [
    # Email addresses.
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED_EMAIL]"),
    # US Social Security numbers (###-##-####).
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    # Credit-card-like numbers: 15 (Amex) or 16 (Visa/MC/Discover) digits,
    # optionally grouped by spaces/dashes. Deliberately narrower than the
    # full 13-19 digit PAN range: logs are full of 13-digit Unix-epoch-ms
    # timestamps, and matching those too would wreck the timestamps a
    # debugging session actually needs.
    (re.compile(r"\b(?:\d[ -]?){15,16}\b"), "[REDACTED_CARD_NUMBER]"),
    # US-style phone numbers, e.g. (555) 123-4567, 555-123-4567, 555.123.4567.
    (re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"), "[REDACTED_PHONE]"),
    # API keys / bearer tokens / secrets accidentally logged, e.g.
    # "Authorization: Bearer eyJhbGciOi..." or "api_key=sk-abc123...".
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|bearer|token|authorization)\b\s*[:=]?\s*"
            r"[\"']?[A-Za-z0-9\-_.]{16,}[\"']?"
        ),
        r"\1=[REDACTED_SECRET]",
    ),
]


def redact_text(text: str) -> str:
    """
    Scrub PII- and secret-shaped substrings out of a plain-text string
    (e.g. raw pod logs). Non-string input is returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_structure(value: Any) -> Any:
    """
    Recursively apply `redact_text` to every string found in a
    JSON-like structure (nested dicts/lists), leaving the shape and
    non-string values (numbers, bools, None) untouched.

    Used as a defense-in-depth backstop in `copilot_tools._serialize` for
    every read tool's output, on top of redaction already applied at the
    client layer (`KubernetesClient.get_pod_logs`, `get_recent_events`) for
    the highest-risk free-text fields.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_structure(val) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_structure(item) for item in value)
    return value
