"""
Runnable sample/demo of the human-in-the-loop remediation flow (Milestone 5+).

Like sample_demo.py, this needs no live cluster, Prometheus, Jaeger, or API
key — the Milestone 3 agents are mocked and the LLM is a scripted fake. It
walks through the exact scenario you'd run for real against a live cluster:

    1. checkout has been scaled to 0 replicas (simulated fault)
    2. You ask the copilot to investigate/fix it
    3. The copilot investigates, identifies the root cause, and calls the
       remediate_scale_deployment tool
    4. The conversation PAUSES here — this is the interrupt() in
       copilot_tools.py firing — and prints exactly what it wants to do
    5. You approve (or reject) at the prompt
    6. Only on approval does kubernetes_agent.scale_deployment get called

Run:
    cd ai-platform/copilot
    PYTHONPATH=".:../tools:../coordinator" python3 sample_remediation_demo.py
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coordinator"))

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent import SRECopilot


def build_mock_agents():
    metrics_agent = MagicMock()
    metrics_agent.get_service_summary.return_value = {
        "service_name": "checkout",
        "request_rate_per_sec": 0.0,
        "error_rate": 1.0,
        "latency_p95_seconds": 0.0,
        "cpu_utilization": None,
        "memory_bytes": None,
    }

    trace_agent = MagicMock()
    trace_agent.get_slow_spans.return_value = []

    kubernetes_agent = MagicMock()
    kubernetes_agent.get_pod_health.return_value = {"unhealthy_pods": [], "high_restart_pods": []}
    kubernetes_agent.get_unhealthy_deployments.return_value = [
        {"name": "checkout", "namespace": "otel-demo", "desired_replicas": 0,
         "ready_replicas": 0, "healthy": False},
    ]
    kubernetes_agent.get_warning_events.return_value = []
    # This is what actually executes when the human approves the fix.
    kubernetes_agent.scale_deployment.return_value = {
        "name": "checkout", "namespace": "otel-demo", "replicas": 1,
    }

    alert_agent = MagicMock()
    alert_agent.get_active_alerts.return_value = [
        {"name": "CheckoutDeploymentUnavailable", "severity": "critical", "status": "firing"},
    ]

    return metrics_agent, trace_agent, kubernetes_agent, alert_agent


def build_investigation_graph_mock():
    graph = MagicMock()
    graph.investigate.return_value = {
        "service_name": "checkout",
        "rca_report": (
            "# Root Cause Analysis Report\n\n"
            "**Affected service:** checkout\n\n"
            "## Likely Root Cause\n"
            "The checkout deployment has been scaled to 0 replicas (desired_replicas=0), "
            "so it has no running pods to serve traffic — every request fails.\n\n"
            "**Confidence:** high\n\n"
            "## Supporting Evidence\n"
            "- checkout deployment: 0/0 ready replicas (desired_replicas explicitly set to 0)\n"
            "- checkout error rate: 100%, request rate: 0 req/s\n\n"
            "## Recommended Actions\n"
            "- Scale the checkout deployment back to its normal replica count (1)\n"
        ),
    }
    return graph


def build_offline_fake_llm():
    """
    Scripted fake LLM: investigate first, then call the remediation tool
    with the root cause as the `reason`, then (after approval/rejection)
    report back to the user.
    """

    class FakeToolCallingModel(BaseChatModel):
        """
        Scripted for the first two turns (investigate, then propose the
        remediation), then — unlike the simpler fixed-sequence fakes used
        elsewhere — actually reads the ToolMessage that comes back after the
        human's approve/reject decision to pick the right closing reply.
        A fixed index-based script would give the same "Done" answer
        regardless of whether the human approved or rejected, which isn't
        what a real model conditioning on tool output would do.
        """

        _calls: list = []

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            call_index = len(self._calls)
            self._calls.append(messages)

            if call_index == 0:
                msg = AIMessage(content="", tool_calls=[
                    {"name": "investigate_service", "args": {"service_name": "checkout"}, "id": "c1"}
                ])
            elif call_index == 1:
                msg = AIMessage(content="", tool_calls=[
                    {
                        "name": "remediate_scale_deployment",
                        "args": {
                            "service_name": "checkout",
                            "desired_replicas": 1,
                            "reason": (
                                "checkout deployment is scaled to 0 replicas (confirmed via "
                                "investigate_service, confidence: high) — this is causing a 100% "
                                "error rate. Scaling back to 1 replica should restore service."
                            ),
                        },
                        "id": "c2",
                    }
                ])
            else:
                last_content = str(messages[-1].content) if messages else ""
                if "cancelled" in last_content.lower() or "not approved" in last_content.lower():
                    msg = AIMessage(
                        content="Understood, I did not make any changes. Let me know if you'd like me to proceed later."
                    )
                else:
                    msg = AIMessage(
                        content="Done — checkout has been scaled back to 1 replica and should recover shortly."
                    )

            return ChatResult(generations=[ChatGeneration(message=msg)])

        def bind_tools(self, tools, **kwargs):
            return self

        @property
        def _llm_type(self):
            return "offline-remediation-demo-fake"

    return FakeToolCallingModel()


def main():
    print("=== Human-in-the-loop remediation demo (fully offline, mocked infra) ===\n")
    print("Scenario: the checkout deployment has been scaled to 0 replicas.\n")

    metrics_agent, trace_agent, kubernetes_agent, alert_agent = build_mock_agents()
    investigation_graph = build_investigation_graph_mock()
    fake_llm = build_offline_fake_llm()

    copilot = SRECopilot(
        metrics_agent=metrics_agent,
        trace_agent=trace_agent,
        kubernetes_agent=kubernetes_agent,
        alert_agent=alert_agent,
        namespace="otel-demo",
        llm=fake_llm,
        investigation_graph=investigation_graph,
    )

    thread_id = "remediation-demo"
    question = "Why is checkout down, and if you can fix it, please do."
    print(f"you> {question}\n")

    reply = copilot.ask(question, thread_id=thread_id)

    if not reply.pending_action:
        print(f"copilot> {reply.content}")
        return

    action = reply.pending_action
    print("--- APPROVAL REQUIRED ---")
    print(f"  Action:            {action['action']}")
    print(f"  Service:           {action['service_name']}")
    print(f"  Desired replicas:  {action['desired_replicas']}")
    print(f"  Reason:            {action['reason']}")
    print("-------------------------\n")

    answer = input("Approve this action? (y/n) > ").strip().lower()
    approved = answer in ("y", "yes")

    final = copilot.respond_to_approval(approved, thread_id=thread_id)
    print(f"\ncopilot> {final.content}\n")

    if approved:
        kubernetes_agent.scale_deployment.assert_called_once_with("otel-demo", "checkout", 1)
        print("[verified] kubernetes_agent.scale_deployment WAS called with (otel-demo, checkout, 1)")
    else:
        kubernetes_agent.scale_deployment.assert_not_called()
        print("[verified] kubernetes_agent.scale_deployment was NOT called")


if __name__ == "__main__":
    main()
