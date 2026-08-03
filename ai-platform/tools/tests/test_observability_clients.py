"""Unit tests for observability clients; no cluster or port-forward is required."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

from alertmanager import AlertmanagerClient, AlertSeverity
from jaeger_client import JaegerClient
from kubernetes_client import KubernetesClient
from prometheus_client import PrometheusClient


class PrometheusClientTests(TestCase):
    def setUp(self):
        self.client = PrometheusClient("http://prometheus.example", timeout=7)
        self.client.session.get = MagicMock()

    def _respond(self, data):
        response = MagicMock()
        response.json.return_value = data
        self.client.session.get.return_value = response

    def test_query_passes_query_and_timeout(self):
        self._respond({"status": "success", "data": {"result": [{"value": [1, "2"]}]}})
        self.assertEqual(self.client.query("up"), [{"value": [1, "2"]}])
        self.client.session.get.assert_called_once_with(
            "http://prometheus.example/api/v1/query", params={"query": "up"}, timeout=7
        )

    def test_cpu_usage_aggregates_all_containers_in_a_pod(self):
        self._respond({"status": "success", "data": {"result": []}})
        self.client.get_pod_cpu_rate("otel-demo", "2m")
        query = self.client.session.get.call_args.kwargs["params"]["query"]
        self.assertIn("sum(rate(container_cpu_usage_seconds_total", query)
        self.assertIn("by (pod)", query)
        self.assertIn("[2m]", query)

    def test_range_query_serializes_datetimes(self):
        self._respond({"status": "success", "data": {"result": []}})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.client.query_range("up", start, start + timedelta(minutes=1), "30s")
        self.assertEqual(self.client.session.get.call_args.kwargs["params"]["step"], "30s")


class JaegerClientTests(TestCase):
    def test_get_services_retries_with_demo_base_path_after_404(self):
        client = JaegerClient("http://jaeger.example")
        missing = MagicMock(status_code=404)
        services = MagicMock(status_code=200)
        services.json.return_value = {"data": ["checkout"]}
        client.session.get = MagicMock(side_effect=[missing, services])
        self.assertEqual(client.get_services(), ["checkout"])
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(
            client.session.get.call_args_list[1].args[0],
            "http://jaeger.example/jaeger/ui/api/services",
        )

    def test_slow_trace_uses_span_timing_when_trace_has_no_duration(self):
        client = JaegerClient()
        client.find_traces = MagicMock(return_value=[
            {"spans": [{"startTime": 1_000_000, "duration": 600_000}]},
            {"spans": [{"startTime": 1_000_000, "duration": 100_000}]},
        ])
        slow = client.find_slow_traces("checkout", min_duration_ms=500)
        self.assertEqual(len(slow), 1)
        client.find_traces.assert_called_once_with("checkout", limit=60)

    def test_get_trace_returns_empty_dict_for_missing_trace(self):
        client = JaegerClient()
        client._get_data = MagicMock(return_value=[])
        self.assertEqual(client.get_trace("missing"), {})


class KubernetesClientTests(TestCase):
    def test_pod_without_ready_condition_is_not_ready(self):
        instance = KubernetesClient.__new__(KubernetesClient)
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="checkout-1", namespace="otel-demo", creation_timestamp=None,
                                     deletion_timestamp=None),
            status=SimpleNamespace(phase="Pending", conditions=[], container_statuses=[]),
        )
        instance.v1 = MagicMock()
        instance.v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
        self.assertFalse(instance.get_pods("otel-demo")[0]["ready"])

    def test_scale_deployment_patches_replica_count(self):
        instance = KubernetesClient.__new__(KubernetesClient)
        instance.apps_v1 = MagicMock()

        result = instance.scale_deployment("otel-demo", "checkout", 1)

        instance.apps_v1.patch_namespaced_deployment_scale.assert_called_once_with(
            name="checkout", namespace="otel-demo", body={"spec": {"replicas": 1}}
        )
        self.assertEqual(result, {"name": "checkout", "namespace": "otel-demo", "replicas": 1})


class AlertmanagerClientTests(TestCase):
    def test_filters_and_classifies_alerts(self):
        client = AlertmanagerClient()
        client.get_active_alerts = MagicMock(return_value=[{
            "name": "CheckoutHighErrors", "severity": "critical",
            "labels": {"job": "checkout"}, "annotations": {},
        }])
        self.assertEqual(len(client.get_alerts_by_severity(AlertSeverity.CRITICAL)), 1)
        self.assertEqual(len(client.get_alerts_by_service("checkout")), 1)
        self.assertTrue(client.classify_alert(client.get_active_alerts()[0])["requires_immediate_action"])

    def test_get_active_alerts_sources_from_prometheus(self):
        client = AlertmanagerClient()
        client.prometheus.get_alerts = MagicMock(return_value=[{
            "labels": {"alertname": "FrontendHighErrorRate", "severity": "critical", "job": "frontend"},
            "annotations": {"description": "error rate high"},
            "state": "firing",
            "activeAt": "2026-01-01T00:00:00Z",
        }])
        alerts = client.get_active_alerts()
        self.assertEqual(alerts, [{
            "name": "FrontendHighErrorRate",
            "status": "firing",
            "severity": "critical",
            "labels": {"alertname": "FrontendHighErrorRate", "severity": "critical", "job": "frontend"},
            "annotations": {"description": "error rate high"},
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
        }])

    def test_get_alert_rules_delegates_to_prometheus(self):
        client = AlertmanagerClient()
        client.prometheus.get_alert_rules = MagicMock(return_value=[{"name": "FrontendHighLatency"}])
        self.assertEqual(client.get_alert_rules(), [{"name": "FrontendHighLatency"}])
