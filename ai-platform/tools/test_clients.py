"""
Smoke tests for the SRE platform observability clients.

Before running, find the service names installed by your chart and port-forward
Prometheus and Jaeger (their names vary by chart release):
    kubectl get svc -n otel-demo
    kubectl port-forward -n otel-demo svc/<prometheus-service> 9090:9090
    kubectl port-forward -n otel-demo svc/<jaeger-service> 16686:16686

Kubernetes client uses your current kubeconfig context, no port-forward needed.
AlertmanagerClient reads from the same Prometheus port-forward above (no
separate Alertmanager service required).

Run:
    uv run python ai-platform/tools/test_clients.py
"""

import os

from dotenv import load_dotenv

from prometheus_client import PrometheusClient
from jaeger_client import JaegerClient
from alertmanager import AlertmanagerClient
from kubernetes_client import KubernetesClient

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")
KUBERNETES_NAMESPACE = os.getenv("KUBERNETES_NAMESPACE", "otel-demo")


def test_prometheus():
    print("\n=== Prometheus ===")
    client = PrometheusClient(PROMETHEUS_URL)
    names = client.get_metric_names()
    print(f"Metric names: {len(names)} found")

    result = client.query("up")
    print(f"'up' query returned {len(result)} series")

    rules = client.get_alert_rules()
    print(f"Alert rules: {len(rules)} found")

    alerts = client.get_alerts()
    print(f"Active Prometheus alerts: {len(alerts)}")


def test_jaeger(service_name: str = "frontend"):
    print("\n=== Jaeger ===")
    client = JaegerClient(JAEGER_URL)
    services = client.get_services()
    print(f"Services: {services}")

    if service_name in services:
        traces = client.find_traces(service=service_name, limit=5)
        print(f"Found {len(traces)} traces for '{service_name}'")
        if traces:
            trace_id = traces[0]["traceID"]
            full_trace = client.get_trace(trace_id)
            print(f"Fetched trace {trace_id}, spans: {len(full_trace.get('spans', []))}")

        slow = client.find_slow_traces(service_name, min_duration_ms=100)
        print(f"Slow traces (>100ms): {len(slow)}")
    else:
        print(f"Service '{service_name}' not found, skipping trace queries")


def test_alertmanager():
    print("\n=== Alertmanager (via Prometheus) ===")
    client = AlertmanagerClient(PROMETHEUS_URL)
    alerts = client.get_active_alerts()
    print(f"Active alerts: {len(alerts)}")
    for a in alerts[:5]:
        print(f"  - {a['name']} [{a['severity']}] status={a['status']}")

    rules = client.get_alert_rules()
    print(f"Alert rules: {len(rules)}")


def test_kubernetes(namespace: str = KUBERNETES_NAMESPACE):
    print("\n=== Kubernetes ===")
    client = KubernetesClient()
    pods = client.get_pods(namespace)
    print(f"Pods in '{namespace}': {len(pods)}")
    for p in pods[:5]:
        print(f"  - {p['name']} status={p['status']} restarts={p['restarts']} age_s={p['age_seconds']:.0f}")

    deployments = client.get_deployments(namespace)
    print(f"Deployments: {len(deployments)}")

    events = client.get_recent_events(namespace, limit=5)
    print(f"Recent events: {len(events)}")


if __name__ == "__main__":
    for fn in (test_prometheus, test_jaeger, test_alertmanager, test_kubernetes):
        try:
            fn()
        except Exception as e:
            print(f"  !! {fn.__name__} failed: {e}")
