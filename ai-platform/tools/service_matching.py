"""
Resolving the affected service name from an alert.

Shared by `coordinator/graph.py::extract_service_name` and
`tools/alert_correlator.py::resolve_alert_service`, which previously each
carried their own copy of both the service list and the matching logic. The
copies had already been flagged as a drift risk in a comment; this module
removes the duplication instead. It lives under `tools/` (the lower layer),
so `coordinator/` can depend on it without the dependency running backwards.

Matching is done on *word boundaries*, not raw substrings. The earlier
implementation squashed the alert name to lowercase-alphanumeric and asked
whether a service name appeared anywhere inside it, which produced false
positives on any alert whose text happened to contain a short service name:

    KubeNodeNotReady          -> "ad"   (from re-AD-y)
    HighLoadAverage           -> "ad"   (from lo-AD-average)
    AlertmanagerFailedReload  -> "ad"   (from relo-AD)

Those are all stock kube-prometheus-stack alerts, and with the Milestone 6
Alertmanager webhook wired up they arrive here automatically — so each one
kicked off a full multi-LLM-call investigation into an unrelated service and
filed a confidently wrong RCA. Splitting the name into tokens first means
"ready" and "ad" are simply different words and no longer match.
"""

import re
from typing import Any, Dict, List, Optional, Sequence


# The OpenTelemetry Demo (Astronomy Shop) services this platform investigates.
DEFAULT_KNOWN_SERVICES = [
    "frontend-proxy", "frontend", "product-catalog", "cart", "checkout", "payment",
    "shipping", "email", "currency", "quote", "accounting", "ad", "recommendation",
    "product-reviews", "image-provider", "fraud-detection", "load-generator",
]

# Splits both CamelCase alert names and hyphen/underscore-separated service
# names into word tokens:
#   "FrontendProxyHighLatency" -> [Frontend, Proxy, High, Latency]
#   "CPUThrottlingHigh"        -> [CPU, Throttling, High]
#   "product-catalog"          -> [product, catalog]
# The leading `[A-Z]+(?![a-z])` branch keeps acronyms intact rather than
# splitting them one letter per token.
_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")


def tokenize(name: str) -> List[str]:
    """Split an alert or service name into lowercase word tokens."""
    if not name:
        return []
    return [token.lower() for token in _TOKEN_RE.findall(name)]


def _contains_sequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True if `needle` appears as a contiguous run of tokens in `haystack`."""
    n, h = len(needle), len(haystack)
    if not n or n > h:
        return False
    return any(list(haystack[i : i + n]) == list(needle) for i in range(h - n + 1))


def match_service_name(
    name: str, known_services: Optional[List[str]] = None
) -> Optional[str]:
    """
    Find the known service referred to by an alert name, or None.

    A service matches when either:
      1. its tokens appear as a contiguous run in the alert's tokens
         ("FrontendProxyHighLatency" -> "frontend-proxy"), or
      2. its squashed form is exactly one of the alert's tokens
         ("LoadgeneratorDown" -> "load-generator"), which covers alert names
         that run a multi-word service together as a single word.

    Candidates are ranked most-specific-first — more tokens, then longer
    name — so "frontend-proxy" wins over "frontend" when both could match.
    """
    tokens = tokenize(name)
    if not tokens:
        return None

    candidates = DEFAULT_KNOWN_SERVICES if known_services is None else known_services
    ranked = sorted(candidates, key=lambda s: (len(tokenize(s)), len(s)), reverse=True)

    for service in ranked:
        service_tokens = tokenize(service)
        if not service_tokens:
            continue
        if _contains_sequence(tokens, service_tokens):
            return service
        if "".join(service_tokens) in tokens:
            return service
    return None


def resolve_service_from_alert(
    alert: Dict[str, Any], known_services: Optional[List[str]] = None
) -> Optional[str]:
    """
    Best-effort extraction of the affected service from an alert.

    Tries, in order:
      1. An explicit `service_name` / `service` / `job` label.
      2. A token match of the alert name against `known_services`.

    Returns None when neither finds anything — which callers should treat as
    "unknown service", not as an invitation to guess. Returning None here is
    the correct outcome for infrastructure alerts like `KubeNodeNotReady`
    that genuinely aren't about any one application service.
    """
    labels = alert.get("labels", {}) or {}
    for key in ("service_name", "service", "job"):
        value = labels.get(key)
        if value:
            return value

    return match_service_name(alert.get("name") or "", known_services)
