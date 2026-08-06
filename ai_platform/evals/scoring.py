"""
Rule-based scoring helpers for RCA/runbook/tool-use evals.

Deliberately not an LLM-judge: everything here is a cheap, deterministic
check (keyword match, substring grounding, regex, trace inspection) so eval
runs are fast, free, and reproducible. This catches the failure modes that
matter most for this platform — a hallucinated root cause, an overconfident
finding on thin evidence, an ungrounded "supporting evidence" bullet, a
runbook with no concrete command, a remediation call with no evidence
gathered first — without needing a second LLM call (and its own flakiness)
to grade the first one. Swap in an LLM-judge later for subtler quality
questions (e.g. "is this explanation actually well-reasoned") if the rule-
based checks prove too coarse.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class FixtureResult:
    fixture_id: str
    checks: List[CheckResult] = field(default_factory=list)
    tokens: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # The actual LLM output (e.g. RCAFinding/RunbookDoc.model_dump()), kept
    # around so a failing check has something to look at beyond "missing
    # required concept(s)" — a keyword-match failure is as often a fixture
    # whose wordlist is too narrow as it is a real model problem, and you
    # can't tell which without seeing what the model actually said.
    raw_output: Optional[Any] = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(c.passed for c in self.checks)


def keyword_hit(text: str, keyword_groups: Sequence[Sequence[str]]) -> CheckResult:
    """
    Passes if, for every group in `keyword_groups`, at least one keyword in
    that group appears (case-insensitive substring) in `text`. Each group is
    an OR of acceptable phrasings for one concept the root cause is expected
    to mention (e.g. `["oom", "out of memory", "oomkilled"]`); multiple
    groups are ANDed, so a fixture can require "mentions OOM AND mentions
    checkout" without pinning exact wording for either.
    """
    text_l = text.lower()
    missing = []
    for group in keyword_groups:
        if not any(kw.lower() in text_l for kw in group):
            missing.append(group)
    if missing:
        return CheckResult("keyword_match", False, f"missing required concept(s): {missing}")
    return CheckResult("keyword_match", True)


def confidence_check(actual: str, expected: Optional[str]) -> CheckResult:
    """
    If the fixture pins an expected confidence (typically "low" for a
    deliberately thin/contradictory evidence fixture — the RCA system prompt
    explicitly promises this), require an exact match. If unset, this check
    always passes (the fixture isn't testing confidence calibration).
    """
    if expected is None:
        return CheckResult("confidence_calibration", True)
    ok = (actual or "").strip().lower() == expected.strip().lower()
    return CheckResult(
        "confidence_calibration", ok, f"expected confidence={expected!r}, got {actual!r}"
    )


_ANCHOR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|\d+(?:\.\d+)?%?")


def _anchors(text: str) -> set:
    """Distinctive tokens (identifiers, numbers) worth checking for grounding."""
    return {tok.lower() for tok in _ANCHOR_RE.findall(text) if len(tok) > 2}


def groundedness_score(bullets: Sequence[str], evidence_blob: str, threshold: float = 0.5) -> CheckResult:
    """
    Best-effort hallucination check: for each bullet in `bullets` (e.g.
    RCAFinding.supporting_evidence), require it to share at least one
    "anchor" token (an identifier or number, not a stopword) with the raw
    evidence text it was supposedly drawn from. Not a semantic check — a
    paraphrase that keeps the actual numbers/names right will still pass,
    which is the point: this flags a bullet that appears to invent a data
    point (a pod name, percentage, count) never present in the evidence.

    Passes if the fraction of grounded bullets is >= `threshold`. An empty
    bullet list passes trivially (nothing to hallucinate).
    """
    if not bullets:
        return CheckResult("groundedness", True, "no evidence bullets to check")

    evidence_anchors = _anchors(evidence_blob)
    grounded = 0
    ungrounded_bullets = []
    for bullet in bullets:
        bullet_anchors = _anchors(bullet)
        if bullet_anchors & evidence_anchors:
            grounded += 1
        else:
            ungrounded_bullets.append(bullet)

    fraction = grounded / len(bullets)
    ok = fraction >= threshold
    detail = f"{grounded}/{len(bullets)} bullets grounded"
    if ungrounded_bullets:
        detail += f"; possibly ungrounded: {ungrounded_bullets}"
    return CheckResult("groundedness", ok, detail)


def contains_pattern(text: str, patterns: Sequence[str], check_name: str) -> CheckResult:
    """Passes if any regex in `patterns` matches `text` (case-insensitive)."""
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return CheckResult(check_name, True)
    return CheckResult(check_name, False, f"no pattern in {list(patterns)} matched")


def diagnosis_first_when_low_confidence(runbook_first_step: str, confidence: str) -> CheckResult:
    """
    RUNBOOK_SYSTEM_PROMPT explicitly requires: if confidence is "low", the
    first step should be further diagnosis rather than jumping to a fix.
    Checked via a loose keyword match on the first step, since "the right
    diagnostic step" isn't otherwise mechanically checkable.
    """
    if confidence.strip().lower() != "low":
        return CheckResult("diagnosis_first", True, "not applicable (confidence not low)")
    diagnostic_terms = ["confirm", "verify", "check", "investigate", "diagnos", "inspect", "review logs", "describe"]
    ok = any(term in runbook_first_step.lower() for term in diagnostic_terms)
    return CheckResult(
        "diagnosis_first", ok,
        f"expected first step to be diagnostic given low confidence, got: {runbook_first_step!r}",
    )


# --- Tool-use / function-call scoring ---------------------------------------


def tool_call_names(steps: Sequence[Dict[str, Any]]) -> List[str]:
    return [s["name"] for s in steps if s.get("type") == "call"]


def expected_tool_used(steps: Sequence[Dict[str, Any]], expected_any: Sequence[str]) -> CheckResult:
    called = tool_call_names(steps)
    ok = any(name in called for name in expected_any)
    return CheckResult(
        "expected_tool_used", ok, f"expected one of {list(expected_any)}, got calls: {called}"
    )


def tool_not_called_before(
    steps: Sequence[Dict[str, Any]], gated_tool: str, must_follow_any_of: Sequence[str]
) -> CheckResult:
    """
    Passes if `gated_tool` was never called, OR if by the time it was first
    called, at least one tool in `must_follow_any_of` had already been
    called earlier in the same trace. Used for the
    remediate_scale_deployment-without-evidence anti-pattern: the platform's
    guardrail blocks the request either way, but a well-behaved model
    shouldn't even attempt it speculatively, and this check catches that
    regardless of whether the guardrail happened to save it.
    """
    called_so_far: List[str] = []
    for step in steps:
        if step.get("type") != "call":
            continue
        name = step["name"]
        if name == gated_tool:
            if any(prior in must_follow_any_of for prior in called_so_far):
                return CheckResult("evidence_before_remediation", True)
            return CheckResult(
                "evidence_before_remediation", False,
                f"{gated_tool} called before any of {list(must_follow_any_of)}; prior calls: {called_so_far}",
            )
        called_so_far.append(name)
    return CheckResult("evidence_before_remediation", True, f"{gated_tool} not called")
