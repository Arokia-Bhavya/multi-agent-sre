"""
Output/quality eval for `ai_platform/coordinator/rca.py`.

For each fixture in `fixtures/rca_cases.py`, builds the real RCA prompt from
canned alert + correlated-findings evidence and calls the real LLM (via
`InvestigationGraph._invoke_structured`, reused as-is rather than
duplicating its retry/fallback logic), then grades the resulting
`RCAFinding` on:

  - keyword_match       — likely_root_cause + supporting_evidence together
                          mention the concept(s) the fixture expects.
  - confidence_calibration — only enforced on fixtures that pin an expected
                          confidence (the thin-evidence case).
  - groundedness        — supporting_evidence bullets aren't inventing data
                          points absent from the correlated findings.

Requires a real API key (ANTHROPIC_API_KEY by default, or GROQ_API_KEY with
LLM_PROVIDER=groq) — this makes real, billed LLM calls. Not part of the
pytest suite; run manually:

    uv run python -m ai_platform.evals.run_rca_evals
"""

import json
import os
import sys
from typing import List
from unittest.mock import MagicMock

from dotenv import load_dotenv

load_dotenv()  # ANTHROPIC_API_KEY / GROQ_API_KEY / LLM_PROVIDER, same as chat.py

from ai_platform.evals.eval_env import apply_eval_llm_overrides

apply_eval_llm_overrides(tier="strong")  # EVAL_LLM_PROVIDER/EVAL_LLM_MODEL, if set — see eval_env.py

from ai_platform.coordinator.graph import InvestigationGraph
from ai_platform.coordinator.rca import RCA_SYSTEM_PROMPT, RCAFinding, build_rca_prompt

from ai_platform.evals.cost_tracking import TokenCostTracker
from ai_platform.evals.fixtures.rca_cases import CASES, RCACase
from ai_platform.evals.scoring import (
    FixtureResult,
    confidence_check,
    groundedness_score,
    keyword_hit,
)


def _graph() -> InvestigationGraph:
    # Agents are never actually invoked here — only .llm / ._invoke_structured
    # are used — so MagicMocks are enough to construct the graph without a
    # live cluster.
    return InvestigationGraph(
        metrics_agent=MagicMock(), trace_agent=MagicMock(), kubernetes_agent=MagicMock(),
    )


def run_case(graph: InvestigationGraph, case: RCACase, tracker: TokenCostTracker) -> FixtureResult:
    prompt = build_rca_prompt(case.alert, case.correlated_findings)
    messages = [
        {"role": "system", "content": RCA_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    tracker.reset()
    tracker.model = os.getenv("LLM_MODEL") or None
    try:
        # Go through the graph's own _invoke_structured rather than calling
        # with_structured_output().invoke() directly, so this eval gets the
        # same retry-then-json_mode-fallback resilience production traffic
        # does for Groq's occasional malformed-function-call response —
        # calling the raw structured-output API here would fail outright on
        # the first flaky response instead of reflecting what actually
        # happens in InvestigationGraph.investigate().
        rca: RCAFinding = graph._invoke_structured(messages, RCAFinding, config={"callbacks": [tracker]})
    except Exception as exc:  # noqa: BLE001
        return FixtureResult(fixture_id=case.case_id, error=f"{type(exc).__name__}: {exc}")

    combined_text = rca.likely_root_cause + " " + " ".join(rca.supporting_evidence)
    evidence_blob = json.dumps(case.correlated_findings, default=str)

    checks = [
        keyword_hit(combined_text, case.root_cause_keywords),
        confidence_check(rca.confidence, case.expected_confidence),
        groundedness_score(rca.supporting_evidence, evidence_blob),
    ]
    return FixtureResult(
        fixture_id=case.case_id, checks=checks, tokens=tracker.summary(), raw_output=rca.model_dump()
    )


def main() -> int:
    graph = _graph()
    tracker = TokenCostTracker()
    results: List[FixtureResult] = [run_case(graph, case, tracker) for case in CASES]

    total_tokens = {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
    print("\n=== RCA quality eval ===\n")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.fixture_id}")
        if result.error:
            print(f"    ERROR: {result.error}")
        for check in result.checks:
            mark = "ok" if check.passed else "XX"
            print(f"    ({mark}) {check.name}" + (f" — {check.detail}" if check.detail else ""))
        if not result.passed and result.raw_output:
            print(f"    raw output: {result.raw_output}")
        if result.tokens:
            print(f"    tokens: {result.tokens}")
            total_tokens["input_tokens"] += result.tokens["input_tokens"]
            total_tokens["output_tokens"] += result.tokens["output_tokens"]
            total_tokens["estimated_cost_usd"] += result.tokens["estimated_cost_usd"]
        print()

    passed = sum(1 for r in results if r.passed)
    print(f"{passed}/{len(results)} fixtures passed")
    print(
        f"total tokens: {total_tokens['input_tokens']} in / {total_tokens['output_tokens']} out "
        f"(~${total_tokens['estimated_cost_usd']:.5f} estimated)"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
