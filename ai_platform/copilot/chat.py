"""
CLI chat interface for the AI SRE Copilot (Milestone 5).

Requires (see .env.example — copy to .env and fill in):
    - ANTHROPIC_API_KEY (default provider), or LLM_PROVIDER=groq + GROQ_API_KEY
    - Prometheus and Jaeger port-forwarded (see ai-platform/tools/test_clients.py)
    - A working kubeconfig context for the target cluster

Run:
    uv run python ai-platform/copilot/chat.py

Then ask things like:
    Why is checkout slow?
    Which service has the highest CPU usage?
    Why did the frontend-proxy become unavailable?
    Show me the latest critical alerts
    Fix the checkout deployment

That last kind of request may pause for your approval before it touches the
cluster — see the "remediate_scale_deployment" tool in copilot_tools.py. When
that happens, you'll be shown exactly what the copilot wants to do and asked
to confirm with y/n before anything actually changes.

Type "exit" or "quit" (or Ctrl-D) to end the session.
"""

import os
import sys
from typing import Optional

from dotenv import load_dotenv


load_dotenv()

from ai_platform.tools.prometheus_client import PrometheusClient
from ai_platform.tools.jaeger_client import JaegerClient
from ai_platform.tools.kubernetes_client import KubernetesClient
from ai_platform.tools.alertmanager import AlertmanagerClient

from ai_platform.tools.metrics_agent import MetricsAgent
from ai_platform.tools.trace_agent import TraceAgent
from ai_platform.tools.kubernetes_agent import KubernetesAgent
from ai_platform.tools.alert_agent import AlertAgent
from ai_platform.tools.incident_store import IncidentStore, DEFAULT_DB_PATH

from ai_platform.copilot.agent import SRECopilot, CopilotReply


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
KUBERNETES_NAMESPACE = os.getenv("KUBERNETES_NAMESPACE", "otel-demo")


def build_copilot(incident_store: Optional[IncidentStore] = None) -> SRECopilot:
    """
    Args:
        incident_store: Optional shared IncidentStore (see
            ai-platform/tools/incident_store.py), so callers that already
            have one (server.py, for its webhook auto-triage registry) can
            hand it in and get a single consistent view instead of a second
            SQLite connection to the same file. If omitted (the CLI's own
            default), a fresh store backed by the same on-disk database
            (IncidentStore.DEFAULT_DB_PATH) is opened, so `chat.py` can
            still answer "what happened with checkout this week"-style
            questions from whatever the web UI's auto-triage has recorded.
    """
    # Default to the same on-disk database the web UI's auto-triage uses
    # (rather than SRECopilot's own :memory: default, which is chosen for
    # test-safety) so the CLI can see incident/RCA history recorded by
    # either entry point.
    incident_store = incident_store or IncidentStore(DEFAULT_DB_PATH)
    return SRECopilot(
        metrics_agent=MetricsAgent(PrometheusClient(PROMETHEUS_URL)),
        trace_agent=TraceAgent(JaegerClient(JAEGER_URL)),
        kubernetes_agent=KubernetesAgent(KubernetesClient()),
        alert_agent=AlertAgent(AlertmanagerClient(PROMETHEUS_URL)),
        namespace=KUBERNETES_NAMESPACE,
        incident_store=incident_store,
    )


def print_pending_action(action: dict) -> None:
    print("\n--- APPROVAL REQUIRED ---")
    print(f"  Action:            {action.get('action')}")
    print(f"  Service:           {action.get('service_name')}")
    print(f"  Desired replicas:  {action.get('desired_replicas')}")
    print(f"  Reason:            {action.get('reason')}")
    print("-------------------------")


def handle_reply(copilot: SRECopilot, reply: CopilotReply, thread_id: str) -> None:
    """
    Print a copilot reply. If it's paused on a pending remediation action,
    prompt for y/n approval right here, resume the turn with that decision,
    and recurse in case the model chains into another interrupt or produces
    a further final answer.
    """
    if reply.pending_action:
        print_pending_action(reply.pending_action)
        answer = input("Approve this action? (y/n) > ").strip().lower()
        approved = answer in ("y", "yes")
        next_reply = copilot.respond_to_approval(approved, thread_id=thread_id)
        handle_reply(copilot, next_reply, thread_id)
        return

    print(f"\ncopilot> {reply.content}\n")


def main():
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    required_key = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    if not os.getenv(required_key):
        print(f"!! LLM_PROVIDER={provider} but {required_key} is not set; the copilot will fail to respond.")

    print("AI SRE Copilot — ask about metrics, traces, Kubernetes health, or active alerts.")
    print("Examples: 'Why is checkout slow?', 'Which service has the highest CPU usage?', "
          "'Show me the latest critical alerts', 'Fix the checkout deployment'")
    print("Type 'exit' or 'quit' to end.\n")

    copilot = build_copilot()
    thread_id = "cli-session"

    while True:
        try:
            question = input("you> ").strip()
        except EOFError:
            print()
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break

        try:
            reply = copilot.ask(question, thread_id=thread_id)
            handle_reply(copilot, reply, thread_id)
        except Exception as e:
            print(f"\ncopilot> (error) {e}\n")


if __name__ == "__main__":
    main()
