"""
Runnable sample/demo for the Milestone 5 AI SRE Copilot.

Unlike chat.py, this does NOT need a live Kubernetes cluster, Prometheus,
or Jaeger — the four Milestone 3 agents are replaced with mocks that return
realistic canned data for a "checkout service crash-looping" incident. This
lets you exercise the full copilot (tool selection + reasoning) with just:

    - Nothing at all -> runs with a scripted fake LLM (fully offline,
      deterministic, no API key needed). Good for a quick smoke test.
    - ANTHROPIC_API_KEY (or LLM_PROVIDER=groq + GROQ_API_KEY) set -> runs
      with the real LLM, so you see genuine tool selection and reasoning
      over the mocked data.

Run:
    cd ai-platform/copilot
    PYTHONPATH=".:../tools:../coordinator" python3 sample_demo.py

Or, with a real key, from the repo root:
    uv run python ai-platform/copilot/sample_demo.py

It walks through the five example queries from the Milestone 5 spec:
    1. Why is checkout slow?
    2. Which service has the highest CPU usage?
    3. Why did the frontend-proxy become unavailable?
    4. Show me the latest critical alerts
    5. What should I do next? (recommended troubleshooting steps)
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coordinator"))

from dotenv import load_dotenv

load_dotenv()

from agent import SRECopilot


# ---- Canned data: a "checkout is crash-looping" incident -------------------

def build_mock_agents():
    metrics_agent = MagicMock()
    metrics_agent.get_service_summary.return_value = {
        "service_name": "checkout",
        "request_rate_per_sec": 41.8,
        "error_rate": 0.34,
        "latency_p95_seconds": 2.1,
        "cpu_utilization": 0.91,
        "memory_bytes": 512_000_000,
    }
    metrics_agent.get_top_cpu_consumers.return_value = [
        {"container_name": "checkout", "cpu_utilization": 0.91},
        {"container_name": "frontend", "cpu_utilization": 0.44},
        {"container_name": "cart", "cpu_utilization": 0.31},
        {"container_name": "product-catalog", "cpu_utilization": 0.18},
        {"container_name": "currency", "cpu_utilization": 0.09},
    ]
    metrics_agent.get_latency_by_route.return_value = {
        "/api/checkout": 2.1,
        "/api/cart": 0.3,
    }

    trace_agent = MagicMock()
    trace_agent.get_slow_spans.return_value = [
        {"trace_id": "t1", "span_id": "s1", "service": "checkout", "operation": "PlaceOrder", "duration_ms": 1890},
        {"trace_id": "t1", "span_id": "s2", "service": "payment", "operation": "Charge", "duration_ms": 120},
    ]
    trace_agent.get_critical_path.return_value = [
        {"span_id": "s1", "service": "checkout", "operation": "PlaceOrder", "duration_ms": 1890},
        {"span_id": "s2", "service": "payment", "operation": "Charge", "duration_ms": 120},
    ]

    kubernetes_agent = MagicMock()
    kubernetes_agent.get_pod_health.return_value = {
        "unhealthy_pods": [
            {"name": "checkout-6f8bcd9c48-b4ss4", "status": "CrashLoopBackOff", "ready": False, "restarts": 9},
        ],
        "high_restart_pods": [
            {"name": "checkout-6f8bcd9c48-b4ss4", "status": "CrashLoopBackOff", "ready": False, "restarts": 9},
        ],
    }
    kubernetes_agent.get_unhealthy_deployments.return_value = [
        {"name": "checkout", "desired_replicas": 1, "ready_replicas": 0, "healthy": False},
    ]
    kubernetes_agent.get_warning_events.return_value = [
        {"type": "Warning", "reason": "BackOff", "object": "Pod/checkout-6f8bcd9c48-b4ss4",
         "message": "Back-off restarting failed container"},
    ]
    kubernetes_agent.get_pod_logs.return_value = (
        "panic: failed to connect to payment service: context deadline exceeded\n"
        "goroutine 1 [running]:\nmain.main()\n\t/src/checkout/main.go:42\n"
    )

    alert_agent = MagicMock()
    alert_agent.get_active_alerts.return_value = [
        {"name": "CheckoutHighErrorRate", "severity": "critical", "status": "firing"},
        {"name": "CheckoutPodCrashLooping", "severity": "critical", "status": "firing"},
    ]
    alert_agent.get_critical_alerts.return_value = alert_agent.get_active_alerts.return_value
    alert_agent.get_incident_summary.return_value = {
        "total_active": 2,
        "critical_count": 2,
        "requires_immediate_action": 2,
        "routes": {"CheckoutHighErrorRate": "pagerduty", "CheckoutPodCrashLooping": "pagerduty"},
    }

    return metrics_agent, trace_agent, kubernetes_agent, alert_agent


def build_investigation_graph_mock():
    """A mock InvestigationGraph so `investigate_service` returns a canned RCA
    report without needing a real LLM call or live cluster."""
    graph = MagicMock()
    graph.investigate.return_value = {
        "service_name": "checkout",
        "rca_report": (
            "# Root Cause Analysis Report\n\n"
            "**Affected service:** checkout\n\n"
            "## Likely Root Cause\n"
            "The checkout pod is crash-looping (9 restarts) after failing to reach the "
            "payment service (connection timeout in pod logs), which is driving the 34% "
            "error rate and 2.1s p95 latency on /api/checkout.\n\n"
            "**Confidence:** high\n\n"
            "## Supporting Evidence\n"
            "- checkout-6f8bcd9c48-b4ss4 is CrashLoopBackOff with 9 restarts\n"
            "- Pod logs show 'failed to connect to payment service: context deadline exceeded'\n"
            "- 34% error rate and 2.1s p95 latency on checkout, concentrated on /api/checkout\n"
            "- Slowest trace span: checkout.PlaceOrder at 1890ms\n\n"
            "## Recommended Actions\n"
            "- Check payment service health and network policy between checkout and payment\n"
            "- Roll back the last checkout deployment if it shipped alongside this regression\n"
            "- Increase checkout's payment-call timeout/retry budget as a stopgap\n"
        ),
    }
    return graph


# ---- Fake offline LLM (used only when no API key is configured) -----------

def build_offline_fake_llm(queries):
    """
    A scripted fake chat model so this demo works with zero setup. Each
    query below maps to exactly one tool call followed by a final answer,
    matching the mocked data above. Only used if no ANTHROPIC_API_KEY /
    GROQ_API_KEY is set.
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    script = {
        "checkout slow": [
            AIMessage(content="", tool_calls=[{"name": "investigate_service", "args": {"service_name": "checkout"}, "id": "c1"}]),
            AIMessage(content=(
                "Checkout is slow because its pod is crash-looping (9 restarts) after failing to "
                "reach the payment service (connection timeout), driving a 34% error rate and 2.1s "
                "p95 latency. Confidence: high. Recommended: check payment service health/network "
                "policy, consider rolling back checkout's last deployment, and raise its payment-call "
                "timeout as a stopgap."
            )),
        ],
        "highest cpu": [
            AIMessage(content="", tool_calls=[{"name": "get_top_cpu_consumers", "args": {"limit": 5}, "id": "c2"}]),
            AIMessage(content="checkout has the highest CPU usage at 91% utilization, well above frontend (44%) and cart (31%)."),
        ],
        "frontend-proxy": [
            AIMessage(content="", tool_calls=[{"name": "get_pod_health", "args": {"service_name": "checkout"}, "id": "c3"}]),
            AIMessage(content=(
                "I don't see any unhealthy frontend-proxy pods in this environment's mocked data — "
                "the only unhealthy pod right now is checkout-6f8bcd9c48-b4ss4 (CrashLoopBackOff, "
                "9 restarts). If frontend-proxy is genuinely down, check its own pod status directly."
            )),
        ],
        "critical alerts": [
            AIMessage(content="", tool_calls=[{"name": "get_critical_alerts", "args": {}, "id": "c4"}]),
            AIMessage(content="2 critical alerts are firing: CheckoutHighErrorRate and CheckoutPodCrashLooping."),
        ],
        "what should i do next": [
            AIMessage(content="", tool_calls=[{"name": "investigate_service", "args": {"service_name": "checkout"}, "id": "c5"}]),
            AIMessage(content=(
                "Next steps: 1) check payment service health and the network policy between checkout "
                "and payment, 2) roll back checkout's last deployment if it shipped with this regression, "
                "3) raise checkout's payment-call timeout/retry budget as a stopgap."
            )),
        ],
    }

    responses = []
    for q in queries:
        key = next(k for k in script if k in q.lower())
        responses.extend(script[key])

    class FakeToolCallingModel(BaseChatModel):
        _responses: list = responses
        _calls: list = []

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            resp = self._responses[len(self._calls)]
            self._calls.append(messages)
            return ChatResult(generations=[ChatGeneration(message=resp)])

        def bind_tools(self, tools, **kwargs):
            return self

        @property
        def _llm_type(self):
            return "offline-demo-fake"

    return FakeToolCallingModel()


def main():
    queries = [
        "Why is checkout slow?",
        "Which service has the highest CPU usage?",
        "Why did the frontend-proxy become unavailable?",
        "Show me the latest critical alerts",
        "What should I do next?",
    ]

    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    required_key = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    have_key = bool(os.getenv(required_key))

    metrics_agent, trace_agent, kubernetes_agent, alert_agent = build_mock_agents()
    investigation_graph = build_investigation_graph_mock()

    if have_key:
        print(f"Using real LLM ({provider}) against mocked infrastructure data.\n")
        copilot = SRECopilot(
            metrics_agent=metrics_agent,
            trace_agent=trace_agent,
            kubernetes_agent=kubernetes_agent,
            alert_agent=alert_agent,
            namespace="otel-demo",
            investigation_graph=investigation_graph,
        )
    else:
        print(f"No {required_key} set — running fully offline with a scripted fake LLM.\n")
        fake_llm = build_offline_fake_llm(queries)
        copilot = SRECopilot(
            metrics_agent=metrics_agent,
            trace_agent=trace_agent,
            kubernetes_agent=kubernetes_agent,
            alert_agent=alert_agent,
            namespace="otel-demo",
            llm=fake_llm,
            investigation_graph=investigation_graph,
        )

    for i, question in enumerate(queries, start=1):
        print(f"[{i}] you> {question}")
        reply = copilot.ask(question, thread_id="demo")
        if reply.pending_action:
            print(f"    copilot> (would pause for approval here: {reply.pending_action})\n")
        else:
            print(f"    copilot> {reply.content}\n")


if __name__ == "__main__":
    main()
