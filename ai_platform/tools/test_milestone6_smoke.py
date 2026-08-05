"""
Smoke test for the Milestone 6 AlertCorrelator and AnomalyDetector.

Uses the same live cluster / port-forward as test_agents_smoke.py:
    kubectl get svc -n otel-demo
    kubectl port-forward -n otel-demo svc/<prometheus-service> 9090:9090

Run:
    uv run python ai_platform/tools/test_milestone6_smoke.py
"""

import os

from dotenv import load_dotenv

from ai_platform.tools.prometheus_http_client import PrometheusClient
from ai_platform.tools.alertmanager import AlertmanagerClient

from ai_platform.tools.alert_agent import AlertAgent
from ai_platform.tools.alert_correlator import AlertCorrelator
from ai_platform.tools.anomaly_detector import AnomalyDetector

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
SERVICE_NAME = os.getenv("SERVICE_NAME", "checkout")


def test_alert_correlator():
    print("\n=== Alert Correlator ===")
    alert_agent = AlertAgent(AlertmanagerClient(PROMETHEUS_URL))
    correlator = AlertCorrelator(alert_agent)

    incidents = correlator.get_correlated_incidents()
    print(f"Correlated incidents: {len(incidents)}")
    for incident in incidents:
        print(
            f"  {incident['service_name']}: {incident['alert_count']} alert(s) "
            f"({', '.join(incident['alert_names'])}), "
            f"max_severity={incident['max_severity']}, "
            f"multi_alert={incident['is_multi_alert']}"
        )
    if not incidents:
        print("  (no active alerts right now — try injecting a failure via the "
              "Feature Flag UI first, e.g. FrontendHighErrorRate)")


def test_anomaly_detector():
    print(f"\n=== Anomaly Detector ('{SERVICE_NAME}') ===")
    detector = AnomalyDetector(PrometheusClient(PROMETHEUS_URL))

    scan = detector.scan_service(SERVICE_NAME)
    print(f"has_anomaly: {scan['has_anomaly']}")
    for metric, result in scan["checks"].items():
        if result.get("reason") == "insufficient history":
            print(f"  {metric}: insufficient history (sample_size={result['sample_size']})")
        else:
            print(
                f"  {metric}: current={result.get('current_value'):.4f} "
                f"baseline_mean={result.get('baseline_mean'):.4f} "
                f"z_score={result.get('z_score'):.2f} "
                f"is_anomaly={result.get('is_anomaly')}"
            )


if __name__ == "__main__":
    for fn in (test_alert_correlator, test_anomaly_detector):
        try:
            fn()
        except Exception as e:
            print(f"  !! {fn.__name__} failed: {e}")
