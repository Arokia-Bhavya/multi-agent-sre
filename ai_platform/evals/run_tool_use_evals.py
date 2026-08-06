"""
Function/tool-call eval for the SRE Copilot (`ai_platform/copilot/agent.py`).

For each fixture in `fixtures/tool_use_cases.py`, builds a real `SRECopilot`
(real LLM, MagicMock agents/investigation_graph returning canned data — no
live cluster) and asks the fixture's question, then grades the resulting
tool-call trace (`CopilotReply.steps`) against:

  - expected_tool_used         — did it call one of the tools a well-routed
                                 answer to this question should use, per
                                 COPILOT_SYSTEM_PROMPT's own routing rules.
  - evidence_before_remediation — for the one anti-pattern fixture: did it
                                 avoid calling remediate_scale_deployment
                                 speculatively, before any evidence-gathering
                                 tool. The platform's own guardrail
                                 (copilot_tools.py) blocks this either way —
                                 this check is about whether the *model*
                                 tried it, independent of the backstop.

This is a pass/fail, no-judgment-call eval (unlike the RCA/runbook evals) —
the tool-call trace is inspected directly, not graded by keyword/grounding
heuristics.

Requires a real API key and makes real, billed LLM calls (one ReAct turn per
fixture, usually 1-3 model round-trips). Not part of the pytest suite; run
manually:

    uv run python -m ai_platform.evals.run_tool_use_evals
"""

import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

from dotenv import load_dotenv

load_dotenv()  # ANTHROPIC_API_KEY / GROQ_API_KEY / LLM_PROVIDER, same as chat.py

from ai_platform.evals.eval_env import apply_eval_llm_overrides

apply_eval_llm_overrides(tier="cheap")  # EVAL_LLM_PROVIDER/EVAL_COPILOT_LLM_MODEL, if set — see eval_env.py

from ai_platform.copilot.agent import SRECopilot, _build_llm

from ai_platform.evals.cost_tracking import TokenCostTracker
from ai_platform.evals.fixtures.tool_use_cases import CASES, ToolUseCase
from ai_platform.evals.scoring import FixtureResult, expected_tool_used, tool_not_called_before


def _apply_returns(agents: Dict[str, MagicMock], agent_returns: Dict[str, Any]) -> None:
    for dotted_method, value in agent_returns.items():
        agent_name, method_name = dotted_method.split(".", 1)
        getattr(agents[agent_name], method_name).return_value = value


def _build_copilot(case: ToolUseCase, tracker: TokenCostTracker) -> SRECopilot:
    agents = {
        "metrics_agent": MagicMock(),
        "trace_agent": MagicMock(),
        "kubernetes_agent": MagicMock(),
        "alert_agent": MagicMock(),
    }
    _apply_returns(agents, case.agent_returns)
    # Backs remediate_scale_deployment's "is this a known deployment"
    # guardrail check, independent of get_deployment_health's own tool
    # (which reads get_unhealthy_deployments instead).
    agents["kubernetes_agent"].get_deployment_health.return_value = case.known_deployments

    investigation_graph = MagicMock()
    investigation_graph.investigate.return_value = {
        "rca_report": "# Root Cause Analysis Report\nLikely root cause: mocked for tool-use eval, see run_rca_evals.py for RCA quality.",
        "service_name": "checkout",
    }
    investigation_graph.generate_runbook.return_value = {
        "runbook_report": "# Mocked Runbook\nSee run_runbook_evals.py for runbook quality.",
        "service_name": "checkout",
    }

    llm = _build_llm().with_config({"callbacks": [tracker]})

    return SRECopilot(
        metrics_agent=agents["metrics_agent"],
        trace_agent=agents["trace_agent"],
        kubernetes_agent=agents["kubernetes_agent"],
        alert_agent=agents["alert_agent"],
        namespace="otel-demo",
        llm=llm,
        investigation_graph=investigation_graph,
    )


def run_case(case: ToolUseCase, tracker: TokenCostTracker) -> FixtureResult:
    tracker.reset()
    try:
        copilot = _build_copilot(case, tracker)
        # max_attempts=1: SRECopilot.ask() retries up to 3x by default on any
        # exception (meant for Groq's occasional malformed-tool-call glitch),
        # but retrying a 429 rate-limit error immediately can't succeed —
        # it only spends two more rejected requests finding that out. Fail
        # fast in eval runs instead of tripling the cost of every
        # already-doomed fixture once a day's quota is exhausted.
        reply = copilot.ask(case.question, thread_id=case.case_id, max_attempts=1)
    except Exception as exc:  # noqa: BLE001
        return FixtureResult(fixture_id=case.case_id, error=f"{type(exc).__name__}: {exc}")

    checks = [expected_tool_used(reply.steps, case.expected_tool_any)]
    if case.evidence_gate:
        gated_tool, must_follow_any_of = case.evidence_gate
        checks.append(tool_not_called_before(reply.steps, gated_tool, must_follow_any_of))

    return FixtureResult(fixture_id=case.case_id, checks=checks, tokens=tracker.summary())


def main() -> int:
    tracker = TokenCostTracker()
    results: List[FixtureResult] = [run_case(case, tracker) for case in CASES]

    total = {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
    print("\n=== Copilot tool-use eval ===\n")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.fixture_id}")
        if result.error:
            print(f"    ERROR: {result.error}")
        for check in result.checks:
            mark = "ok" if check.passed else "XX"
            print(f"    ({mark}) {check.name}" + (f" — {check.detail}" if check.detail else ""))
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
