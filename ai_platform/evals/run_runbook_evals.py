"""
Output/quality eval for `ai_platform/coordinator/runbook.py`.

Same fixtures as `run_rca_evals.py` (`fixtures/rca_cases.py`), since a
runbook is always generated from an RCA finding for the same evidence. Each
case first gets a real RCAFinding (same call `run_rca_evals.py` makes), then
feeds it into a second real LLM call for the RunbookDoc, and grades that on:

  - command_pattern   — at least one step contains a concrete kubectl/
                        PromQL-shaped command, per `command_patterns` in the
                        fixture (RUNBOOK_SYSTEM_PROMPT explicitly asks for
                        this "where the evidence makes one obvious").
  - diagnosis_first    — only enforced when the case's expected confidence
                        is "low": the prompt requires the first step be
                        further diagnosis, not a jump straight to a fix.

Requires a real API key and makes real, billed LLM calls (two per fixture —
RCA then runbook). Not part of the pytest suite; run manually:

    uv run python -m ai_platform.evals.run_runbook_evals
"""

import sys
from typing import List
from unittest.mock import MagicMock

from dotenv import load_dotenv

load_dotenv()  # ANTHROPIC_API_KEY / GROQ_API_KEY / LLM_PROVIDER, same as chat.py

from ai_platform.evals.eval_env import apply_eval_llm_overrides

apply_eval_llm_overrides(tier="strong")  # EVAL_LLM_PROVIDER/EVAL_LLM_MODEL, if set — see eval_env.py

from ai_platform.coordinator.graph import InvestigationGraph
from ai_platform.coordinator.rca import RCA_SYSTEM_PROMPT, RCAFinding, build_rca_prompt
from ai_platform.coordinator.runbook import RUNBOOK_SYSTEM_PROMPT, RunbookDoc, build_runbook_prompt

from ai_platform.evals.cost_tracking import TokenCostTracker
from ai_platform.evals.fixtures.rca_cases import CASES, RCACase
from ai_platform.evals.scoring import FixtureResult, contains_pattern, diagnosis_first_when_low_confidence


def _graph() -> InvestigationGraph:
    return InvestigationGraph(
        metrics_agent=MagicMock(), trace_agent=MagicMock(), kubernetes_agent=MagicMock(),
    )


def run_case(graph: InvestigationGraph, case: RCACase, tracker: TokenCostTracker) -> FixtureResult:
    tracker.reset()
    try:
        # Go through the graph's own _invoke_structured (not a raw
        # with_structured_output().invoke()) for both calls below, so this
        # eval gets the same retry-then-json_mode-fallback resilience
        # production traffic does for Groq's occasional malformed-function-
        # call response, instead of failing outright on the first flaky one.
        rca_messages = [
            {"role": "system", "content": RCA_SYSTEM_PROMPT},
            {"role": "user", "content": build_rca_prompt(case.alert, case.correlated_findings)},
        ]
        rca: RCAFinding = graph._invoke_structured(rca_messages, RCAFinding, config={"callbacks": [tracker]})

        service_name = case.correlated_findings.get("service_name")
        runbook_messages = [
            {"role": "system", "content": RUNBOOK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_runbook_prompt(case.alert, service_name, case.correlated_findings, rca),
            },
        ]
        runbook: RunbookDoc = graph._invoke_structured(
            runbook_messages, RunbookDoc, config={"callbacks": [tracker]}
        )
    except Exception as exc:  # noqa: BLE001
        return FixtureResult(fixture_id=case.case_id, error=f"{type(exc).__name__}: {exc}")

    all_steps_text = " ".join(runbook.steps)
    checks = [
        contains_pattern(all_steps_text, case.command_patterns, "command_pattern")
        if case.command_patterns
        else None,
        diagnosis_first_when_low_confidence(runbook.steps[0] if runbook.steps else "", rca.confidence),
    ]
    checks = [c for c in checks if c is not None]
    return FixtureResult(
        fixture_id=case.case_id, checks=checks, tokens=tracker.summary(), raw_output=runbook.model_dump()
    )


def main() -> int:
    graph = _graph()
    tracker = TokenCostTracker()
    results: List[FixtureResult] = [run_case(graph, case, tracker) for case in CASES]

    total = {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
    print("\n=== Runbook quality eval ===\n")
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
            total["input_tokens"] += result.tokens["input_tokens"]
            total["output_tokens"] += result.tokens["output_tokens"]
            total["estimated_cost_usd"] += result.tokens["estimated_cost_usd"]
        print()

    passed = sum(1 for r in results if r.passed)
    print(f"{passed}/{len(results)} fixtures passed")
    print(
        f"total tokens: {total['input_tokens']} in / {total['output_tokens']} out "
        f"(~${total['estimated_cost_usd']:.5f} estimated)"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
