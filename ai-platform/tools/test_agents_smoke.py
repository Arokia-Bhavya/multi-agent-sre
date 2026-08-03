"""
Smoke tests for the Milestone 3 agents (Metrics, Trace, Kubernetes, Alert).

Uses the same live cluster / port-forwards as test_clients.py:
    kubectl get svc -n otel-demo
    kubectl port-forward -n otel-demo svc/<prometheus-service> 9090:9090
    kubectl port-forward -n otel-demo svc/<jaeger-service> 16686:16686

Kubernetes access uses your current kubeconfig context, no port-forward needed.

Run:
    uv run python ai-platform/tools/test_agents_smoke.py
"""

import os

from dotenv import load_dotenv

from prometheus_client import PrometheusClient
from jaeger_client import JaegerClient
from kubernetes_client import KubernetesClient
from alertmanager import AlertmanagerClient

from metrics_agent import MetricsAgent
from trace_agent import TraceAgent
from kubernetes_agent import KubernetesAgent
from alert_agent import AlertAgent

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
KUBERNETES_NAMESPACE = os.getenv("KUBERNETES_NAMESPACE", "otel-demo")
SERVICE_NAME = os.getenv("SERVICE_NAME", "checkout")


def test_metrics_agent():
    print("\n=== Metrics Agent ===")
    agent = MetricsAgent(PrometheusClient(PROMETHEUS_URL))
    summary = agent.get_service_summary(SERVICE_NAME)
    print(f"'{SERVICE_NAME}' summary: {summary}")

    top_cpu = agent.get_top_cpu_consumers(limit=5)
    print(f"Top CPU consumers: {len(top_cpu)}")
    for c in top_cpu:
        print(f"  {c['container_name']}: {c['cpu_utilization']:.3f}")


def test_trace_agent():
    print("\n=== Trace Agent ===")
    agent = TraceAgent(JaegerClient(JAEGER_URL))

    slow_spans = agent.get_slow_spans(SERVICE_NAME, min_duration_ms=50)
    print(f"Slow spans (>50ms) for '{SERVICE_NAME}': {len(slow_spans)}")
    for span in slow_spans[:5]:
        print(f"  {span['service']}.{span['operation']}: {span['duration_ms']:.1f}ms")

    traces = agent.get_traces(SERVICE_NAME, limit=1)
    if traces:
        summary = agent.get_trace_summary(traces[0]["traceID"])
        print(f"Trace summary: span_count={summary['span_count']} duration_ms={summary['duration_ms']:.1f}")
        print("Critical path:")
        for span in summary["critical_path"]:
            print(f"  {span['service']}.{span['operation']}: {span['duration_ms']:.1f}ms")
    else:
        print(f"No traces found for '{SERVICE_NAME}', skipping trace summary")


def test_kubernetes_agent(namespace: str = KUBERNETES_NAMESPACE):
    print("\n=== Kubernetes Agent ===")
    agent = KubernetesAgent(KubernetesClient())

    summary = agent.get_namespace_summary(namespace)
    print(f"Namespace summary: {summary}")

    unhealthy = agent.get_unhealthy_pods(namespace)
    print(f"Unhealthy pods: {len(unhealthy)}")
    for pod in unhealthy[:5]:
        print(f"  {pod['name']}: status={pod['status']} ready={pod['ready']}")

    warnings = agent.get_warning_events(namespace, limit=5)
    print(f"Warning events: {len(warnings)}")


def test_alert_agent():
    print("\n=== Alert Agent ===")
    agent = AlertAgent(AlertmanagerClient(PROMETHEUS_URL))

    summary = agent.get_incident_summary()
    print(f"Incident summary: {summary}")


if __name__ == "__main__":
    for fn in (test_metrics_agent, test_trace_agent, test_kubernetes_agent, test_alert_agent):
        try:
            fn()
        except Exception as e:
            print(f"  !! {fn.__name__} failed: {e}")
