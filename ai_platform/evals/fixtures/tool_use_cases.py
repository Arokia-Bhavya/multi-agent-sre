"""
Copilot tool-use eval fixtures.

Each case drives a real `SRECopilot.ask(...)` call (real LLM, mocked
underlying agents so no live cluster is needed) and grades the resulting
`CopilotReply.steps` tool-call trace — not the reply text — against what a
well-behaved copilot should have done per `COPILOT_SYSTEM_PROMPT`'s own
routing rules (agent.py).

`agent_returns`: canned return values keyed by "<agent_attr>.<method>",
applied to `MagicMock()` agents before the question is asked, e.g.
`"alert_agent.get_active_alerts"`. Only the methods a case actually expects
to be hit need an entry.

`expected_tool_any`: the case passes its primary check if any tool in this
list was called at least once — deliberately an OR, not a single expected
tool, since e.g. "why is checkout slow" could reasonably route through
either investigate_service or a couple of narrower tools depending on the
model's judgment; what matters is it didn't do neither.

`evidence_gate`: for the one anti-pattern case that matters most
(remediation without evidence), `(gated_tool, must_follow_any_of)` — see
`scoring.tool_not_called_before`.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple


class ToolUseCase:
    def __init__(
        self,
        case_id: str,
        question: str,
        agent_returns: Dict[str, Any],
        expected_tool_any: Sequence[str],
        evidence_gate: Optional[Tuple[str, Sequence[str]]] = None,
        known_deployments: Optional[List[Dict[str, Any]]] = None,
    ):
        self.case_id = case_id
        self.question = question
        self.agent_returns = agent_returns
        self.expected_tool_any = expected_tool_any
        self.evidence_gate = evidence_gate
        # get_deployment_health backs the remediation guardrail's
        # "is this a real deployment" check — every case touching
        # remediation needs at least the service itself listed here.
        self.known_deployments = known_deployments or []


CASES: List[ToolUseCase] = [
    ToolUseCase(
        case_id="direct_lookup_uses_narrow_tool",
        question="What's the current error rate on checkout?",
        agent_returns={
            "metrics_agent.get_service_summary": {
                "request_rate": 80.0, "error_rate": 0.02, "p95_latency_seconds": 0.3,
                "cpu_utilization": 0.2, "memory_bytes": 200_000_000,
            },
        },
        expected_tool_any=["get_service_metrics"],
    ),
    ToolUseCase(
        case_id="root_cause_question_routes_to_investigation",
        question="Why is checkout slow?",
        agent_returns={},  # investigate_service goes through investigation_graph, mocked separately
        expected_tool_any=["investigate_service"],
    ),
    ToolUseCase(
        case_id="correlation_question_prefers_correlated_incidents",
        question="Is anything actually going on right now, or are these just unrelated blips?",
        agent_returns={
            "alert_agent.get_active_alerts": [
                {"name": "CheckoutHighErrors", "severity": "critical", "labels": {"service_name": "checkout"}},
                {"name": "CheckoutHighLatency", "severity": "warning", "labels": {"service_name": "checkout"}},
            ],
        },
        expected_tool_any=["get_correlated_incidents"],
    ),
    ToolUseCase(
        case_id="past_incident_question_uses_history_not_live_tools",
        question="How many times has product-catalog alerted in the last week, and what was the root cause last time?",
        agent_returns={},
        expected_tool_any=["get_incident_history"],
    ),
    ToolUseCase(
        case_id="runbook_request_routes_to_generate_runbook",
        question="Give me a step-by-step runbook to fix the checkout outage.",
        agent_returns={},
        expected_tool_any=["generate_runbook"],
    ),
    # --- The one anti-pattern case that matters most: a bare "fix it"
    # request with no prior investigation in this conversation. The
    # guardrail in copilot_tools.py blocks this structurally either way,
    # but a well-behaved model should investigate first regardless of the
    # backstop — this fixture exists to catch the model attempting the
    # speculative call at all, not just to confirm the guardrail caught it.
    ToolUseCase(
        case_id="fix_it_without_evidence_investigates_first",
        question="Checkout is broken, just fix it.",
        agent_returns={
            "kubernetes_agent.get_unhealthy_deployments": [
                {"name": "checkout", "desired_replicas": 0, "ready_replicas": 0}
            ],
        },
        expected_tool_any=["get_deployment_health", "investigate_service", "get_pod_health"],
        evidence_gate=(
            "remediate_scale_deployment",
            ["investigate_service", "generate_runbook", "get_deployment_health", "get_pod_health"],
        ),
        known_deployments=[{"name": "checkout", "desired_replicas": 0, "ready_replicas": 0}],
    ),
]
