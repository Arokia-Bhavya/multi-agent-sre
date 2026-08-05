"""Unit tests for Milestone 3 agents; no cluster or port-forward is required."""

from unittest import TestCase
from unittest.mock import MagicMock

from ai_platform.tools.alertmanager import AlertmanagerClient
from ai_platform.tools.jaeger_client import JaegerClient
from ai_platform.tools.kubernetes_client import KubernetesClient
from ai_platform.tools.prometheus_http_client import PrometheusClient

from ai_platform.tools.alert_agent import AlertAgent
from ai_platform.tools.kubernetes_agent import KubernetesAgent
from ai_platform.tools.metrics_agent import MetricsAgent
from ai_platform.tools.trace_agent import TraceAgent


def _prom_result(value):
    return [{"metric": {}, "value": [0, str(value)]}]


class MetricsAgentTests(TestCase):
    def setUp(self):
        self.prom = PrometheusClient("http://prometheus.example")
        self.agent = MetricsAgent(self.prom)

    def test_get_request_rate_reads_first_series(self):
        self.prom.query = MagicMock(return_value=_prom_result(12.5))
        self.assertEqual(self.agent.get_request_rate("checkout"), 12.5)

    def test_get_request_rate_defaults_to_zero_when_no_series(self):
        self.prom.query = MagicMock(return_value=[])
        self.assertEqual(self.agent.get_request_rate("checkout"), 0.0)

    def test_get_error_rate_divides_errors_by_total(self):
        self.prom.query = MagicMock(side_effect=[_prom_result(100), _prom_result(5)])
        self.assertEqual(self.agent.get_error_rate("checkout"), 0.05)

    def test_get_error_rate_is_zero_when_no_traffic(self):
        self.prom.query = MagicMock(side_effect=[[], []])
        self.assertEqual(self.agent.get_error_rate("checkout"), 0.0)

    def test_get_latency_percentile_handles_nan(self):
        self.prom.query = MagicMock(return_value=_prom_result("NaN"))
        self.assertEqual(self.agent.get_latency_percentile("checkout"), 0.0)

    def test_get_request_rate_escapes_promql_injection_in_service_name(self):
        # A service_name containing a `"` should not be able to break out of
        # the label matcher and inject extra PromQL — see prometheus_http_client.
        # escape_label_value. Regression for the PromQL injection finding.
        self.prom.query = MagicMock(return_value=_prom_result(1.0))
        malicious = 'checkout"} or sum(rate(secret_metric[5m])) or vector(1'

        self.agent.get_request_rate(malicious)

        query = self.prom.query.call_args[0][0]
        self.assertNotIn('checkout"} or', query)
        self.assertIn('service_name="checkout\\"} or sum(rate(secret_metric[5m])) or vector(1"', query)

    def test_get_cpu_usage_returns_none_when_missing(self):
        self.prom.query = MagicMock(return_value=[])
        self.assertIsNone(self.agent.get_cpu_usage("checkout"))

    def test_get_service_summary_combines_all_metrics(self):
        self.prom.query = MagicMock(return_value=_prom_result(1))
        summary = self.agent.get_service_summary("checkout")
        self.assertEqual(summary["service_name"], "checkout")
        self.assertIn("request_rate_per_sec", summary)
        self.assertIn("latency_p95_seconds", summary)


class TraceAgentTests(TestCase):
    def setUp(self):
        self.jaeger = JaegerClient("http://jaeger.example")
        self.agent = TraceAgent(self.jaeger)

    def test_get_slow_spans_filters_and_sorts_by_duration(self):
        self.jaeger.find_traces = MagicMock(return_value=[
            {
                "traceID": "t1",
                "processes": {"p1": {"serviceName": "checkout"}},
                "spans": [
                    {"spanID": "s1", "processID": "p1", "operationName": "fast", "duration": 10_000},
                    {"spanID": "s2", "processID": "p1", "operationName": "slow", "duration": 800_000},
                ],
            }
        ])
        slow = self.agent.get_slow_spans("checkout", min_duration_ms=100)
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0]["operation"], "slow")
        self.assertEqual(slow[0]["duration_ms"], 800.0)

    def test_get_critical_path_follows_latest_finishing_child(self):
        self.jaeger.get_trace = MagicMock(return_value={
            "processes": {"p1": {"serviceName": "checkout"}},
            "spans": [
                {"spanID": "root", "processID": "p1", "operationName": "handle", "startTime": 0,
                 "duration": 1000, "references": []},
                {"spanID": "child-fast", "processID": "p1", "operationName": "cache", "startTime": 100,
                 "duration": 50, "references": [{"refType": "CHILD_OF", "spanID": "root"}]},
                {"spanID": "child-slow", "processID": "p1", "operationName": "db", "startTime": 200,
                 "duration": 700, "references": [{"refType": "CHILD_OF", "spanID": "root"}]},
            ],
        })
        path = self.agent.get_critical_path("trace-1")
        self.assertEqual([p["span_id"] for p in path], ["root", "child-slow"])

    def test_get_critical_path_returns_empty_for_missing_trace(self):
        self.jaeger.get_trace = MagicMock(return_value={})
        self.assertEqual(self.agent.get_critical_path("missing"), [])

    def test_get_trace_summary_reports_not_found(self):
        self.jaeger.get_trace = MagicMock(return_value={})
        summary = self.agent.get_trace_summary("missing")
        self.assertFalse(summary["found"])


class KubernetesAgentTests(TestCase):
    def setUp(self):
        self.k8s = KubernetesClient.__new__(KubernetesClient)
        self.agent = KubernetesAgent(self.k8s)

    def test_get_unhealthy_pods_filters_not_running_or_not_ready(self):
        self.k8s.get_pods = MagicMock(return_value=[
            {"name": "a", "status": "Running", "ready": True, "restarts": 0},
            {"name": "b", "status": "Running", "ready": False, "restarts": 0},
            {"name": "c", "status": "Pending", "ready": False, "restarts": 0},
        ])
        unhealthy = self.agent.get_unhealthy_pods("otel-demo")
        self.assertEqual({p["name"] for p in unhealthy}, {"b", "c"})

    def test_get_high_restart_pods_uses_threshold(self):
        self.k8s.get_pods = MagicMock(return_value=[
            {"name": "a", "status": "Running", "ready": True, "restarts": 1},
            {"name": "b", "status": "Running", "ready": True, "restarts": 5},
        ])
        high = self.agent.get_high_restart_pods("otel-demo", threshold=3)
        self.assertEqual([p["name"] for p in high], ["b"])

    def test_get_unhealthy_deployments_flags_missing_replicas(self):
        self.k8s.get_deployments = MagicMock(return_value=[
            {"name": "checkout", "desired_replicas": 2, "ready_replicas": 2,
             "updated_replicas": 2, "available_replicas": 2},
            {"name": "cart", "desired_replicas": 2, "ready_replicas": 1,
             "updated_replicas": 2, "available_replicas": 1},
        ])
        unhealthy = self.agent.get_unhealthy_deployments("otel-demo")
        self.assertEqual([d["name"] for d in unhealthy], ["cart"])

    def test_get_warning_events_filters_by_type(self):
        self.k8s.get_recent_events = MagicMock(return_value=[
            {"type": "Normal", "reason": "Scheduled"},
            {"type": "Warning", "reason": "FailedMount"},
        ])
        warnings = self.agent.get_warning_events("otel-demo")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["reason"], "FailedMount")

    def test_scale_deployment_delegates_to_client(self):
        self.k8s.scale_deployment = MagicMock(
            return_value={"name": "checkout", "namespace": "otel-demo", "replicas": 1}
        )
        result = self.agent.scale_deployment("otel-demo", "checkout", 1)
        self.k8s.scale_deployment.assert_called_once_with("otel-demo", "checkout", 1)
        self.assertEqual(result["replicas"], 1)

    def test_get_namespace_summary_counts_problems(self):
        self.k8s.get_pods = MagicMock(return_value=[
            {"name": "a", "status": "Running", "ready": True, "restarts": 0},
            {"name": "b", "status": "Running", "ready": False, "restarts": 5},
        ])
        self.k8s.get_deployments = MagicMock(return_value=[])
        summary = self.agent.get_namespace_summary("otel-demo")
        self.assertEqual(summary["total_pods"], 2)
        self.assertEqual(summary["unhealthy_pods"], 1)
        self.assertEqual(summary["high_restart_pods"], 1)


class AlertAgentTests(TestCase):
    def setUp(self):
        self.alertmanager = AlertmanagerClient()
        self.agent = AlertAgent(self.alertmanager)

    def test_route_alert_maps_severity_to_channel(self):
        self.assertEqual(self.agent.route_alert({"severity": "critical"}), "pagerduty")
        self.assertEqual(self.agent.route_alert({"severity": "medium"}), "slack-alerts")
        self.assertEqual(self.agent.route_alert({}), "log-only")

    def test_classify_incidents_delegates_to_client(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            {"name": "CheckoutHighErrors", "severity": "critical", "labels": {}, "annotations": {}},
        ])
        classified = self.agent.classify_incidents()
        self.assertEqual(len(classified), 1)
        self.assertTrue(classified[0]["is_critical"])

    def test_get_all_alert_rules_delegates_to_client(self):
        self.alertmanager.get_alert_rules = MagicMock(return_value=[
            {"name": "ProductCatalogPodDown", "state": "firing", "labels": {}, "annotations": {}},
            {"name": "FrontendHighLatency", "state": "inactive", "labels": {}, "annotations": {}},
        ])
        rules = self.agent.get_all_alert_rules()
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["state"], "firing")
        self.alertmanager.get_alert_rules.assert_called_once()

    def test_get_incident_summary_aggregates_counts_and_routes(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            {"name": "CheckoutHighErrors", "severity": "critical", "labels": {}, "annotations": {}},
            {"name": "CartHighLatency", "severity": "medium", "labels": {}, "annotations": {}},
        ])
        summary = self.agent.get_incident_summary()
        self.assertEqual(summary["total_active"], 2)
        self.assertEqual(summary["critical_count"], 1)
        self.assertEqual(summary["routes"]["CheckoutHighErrors"], "pagerduty")
        self.assertEqual(summary["routes"]["CartHighLatency"], "slack-alerts")
