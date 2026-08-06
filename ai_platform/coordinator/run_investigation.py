"""
Smoke test / CLI for the Milestone 4 LangGraph coordinator.

Requires (see .env.example — copy to .env and fill in):
    - ANTHROPIC_API_KEY (default provider), or LLM_PROVIDER=groq + GROQ_API_KEY
    - Prometheus and Jaeger port-forwarded (see ai-platform/tools/test_clients.py)
    - A working kubeconfig context for the target cluster

Run against the first active Prometheus alert:
    uv run python ai-platform/coordinator/run_investigation.py

Run against a specific alert name (useful when nothing is currently firing;
this builds a synthetic alert so you can still exercise the full pipeline):
    uv run python ai-platform/coordinator/run_investigation.py --alert-name FrontendHighErrorRate --service frontend
"""

import argparse
import os
import sys

from dotenv import load_dotenv


load_dotenv()

from ai_platform.tools.prometheus_http_client import PrometheusClient
from ai_platform.tools.jaeger_client import JaegerClient
from ai_platform.tools.kubernetes_client import KubernetesClient
from ai_platform.tools.alertmanager import AlertmanagerClient

from ai_platform.tools.metrics_agent import MetricsAgent
from ai_platform.tools.trace_agent import TraceAgent
from ai_platform.tools.kubernetes_agent import KubernetesAgent

from ai_platform.coordinator.graph import InvestigationGraph


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
KUBERNETES_NAMESPACE = os.getenv("KUBERNETES_NAMESPACE", "otel-demo")


def build_graph() -> InvestigationGraph:
    return InvestigationGraph(
        metrics_agent=MetricsAgent(PrometheusClient(PROMETHEUS_URL)),
        trace_agent=TraceAgent(JaegerClient(JAEGER_URL)),
        kubernetes_agent=KubernetesAgent(KubernetesClient()),
        namespace=KUBERNETES_NAMESPACE,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert-name", help="Use a synthetic alert with this name instead of a live one")
    parser.add_argument("--service", help="Service name label for the synthetic alert")
    parser.add_argument("--severity", default="critical", help="Severity label for the synthetic alert")
    args = parser.parse_args()

    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    required_key = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    if not os.getenv(required_key):
        print(f"!! LLM_PROVIDER={provider} but {required_key} is not set; produce_rca_report will fail.")

    if args.alert_name:
        alert = {
            "name": args.alert_name,
            "severity": args.severity,
            "status": "firing",
            "labels": ({"service_name": args.service} if args.service else {}),
            "annotations": {},
        }
    else:
        print("No --alert-name given; looking for a live active alert via Prometheus...")
        alert_client = AlertmanagerClient(PROMETHEUS_URL)
        active = alert_client.get_active_alerts()
        if not active:
            print("No active alerts found. Pass --alert-name (and --service) to run against a synthetic one.")
            return
        alert = active[0]
        print(f"Using live alert: {alert['name']}")

    graph = build_graph()
    result = graph.investigate(alert)

    print(f"\nResolved service: {result.get('service_name')}")
    if result.get("errors"):
        print(f"Errors: {result['errors']}")

    print("\n" + result.get("rca_report", "(no report generated)"))


if __name__ == "__main__":
    main()
