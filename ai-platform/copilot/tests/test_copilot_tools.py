"""
Unit tests for the Milestone 5 copilot tool layer.

No cluster, network, or API key is required: the underlying agents (and the
Milestone 4 InvestigationGraph) are replaced with MagicMocks.
"""

import json
import os
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

from copilot_tools import build_copilot_tools
from incident_store import IncidentStore


class CopilotToolsTests(TestCase):
    def setUp(self):
        self.metrics_agent = MagicMock()
        self.trace_agent = MagicMock()
        self.kubernetes_agent = MagicMock()
        self.alert_agent = MagicMock()
        self.investigation_graph = MagicMock()
        self.alert_correlator = MagicMock()
        self.anomaly_detector = MagicMock()
        self.incident_store = IncidentStore()  # real (":memory:") — cheap, no mocking needed

        self.tool_map = {
            t.name: t
            for t in build_copilot_tools(
                metrics_agent=self.metrics_agent,
                trace_agent=self.trace_agent,
                kubernetes_agent=self.kubernetes_agent,
                alert_agent=self.alert_agent,
                investigation_graph=self.investigation_graph,
                namespace="otel-demo",
                alert_correlator=self.alert_correlator,
                anomaly_detector=self.anomaly_detector,
                incident_store=self.incident_store,
            )
        }

    def _invoke(self, name, **kwargs):
        """Invoke a tool and parse its JSON-string return value back into Python."""
        raw = self.tool_map[name].invoke(kwargs)
        return json.loads(raw)

    def test_all_expected_tools_are_registered(self):
        expected = {
            "get_service_metrics",
            "get_top_cpu_consumers",
            "get_latency_by_route",
            "get_slow_traces",
            "get_trace_critical_path",
            "get_pod_health",
            "get_deployment_health",
            "get_recent_k8s_events",
            "get_pod_logs",
            "get_active_alerts",
            "get_critical_alerts",
            "get_incident_summary",
            "get_correlated_incidents",
            "get_incident_history",
            "search_similar_incidents",
            "detect_service_anomalies",
            "investigate_service",
            "generate_runbook",
            "remediate_scale_deployment",
        }
        self.assertEqual(expected, set(self.tool_map))

    def test_get_service_metrics_delegates_to_metrics_agent(self):
        self.metrics_agent.get_service_summary.return_value = {"service_name": "checkout", "error_rate": 0.1}
        result = self._invoke("get_service_metrics", service_name="checkout")
        self.metrics_agent.get_service_summary.assert_called_once_with("checkout")
        self.assertEqual(result["error_rate"], 0.1)

    def test_get_top_cpu_consumers_passes_limit_through(self):
        self.metrics_agent.get_top_cpu_consumers.return_value = [{"container_name": "checkout", "cpu_utilization": 0.9}]
        result = self._invoke("get_top_cpu_consumers", limit=3)
        self.metrics_agent.get_top_cpu_consumers.assert_called_once_with(3)
        self.assertEqual(result[0]["container_name"], "checkout")

    def test_get_slow_traces_delegates_to_trace_agent(self):
        self.trace_agent.get_slow_spans.return_value = [{"trace_id": "t1", "duration_ms": 900}]
        result = self._invoke("get_slow_traces", service_name="checkout", min_duration_ms=200)
        self.trace_agent.get_slow_spans.assert_called_once_with("checkout", min_duration_ms=200)
        self.assertEqual(result[0]["trace_id"], "t1")

    def test_get_trace_critical_path_delegates_to_trace_agent(self):
        self.trace_agent.get_critical_path.return_value = [{"service": "checkout", "duration_ms": 900}]
        result = self._invoke("get_trace_critical_path", trace_id="t1")
        self.trace_agent.get_critical_path.assert_called_once_with("t1")
        self.assertEqual(result[0]["service"], "checkout")

    def test_get_pod_health_without_service_filter_returns_everything(self):
        self.kubernetes_agent.get_pod_health.return_value = {
            "unhealthy_pods": [{"name": "checkout-abc"}, {"name": "cart-xyz"}],
            "high_restart_pods": [{"name": "checkout-abc"}],
        }
        result = self._invoke("get_pod_health")
        self.assertEqual(len(result["unhealthy_pods"]), 2)
        self.assertEqual(len(result["high_restart_pods"]), 1)

    def test_get_pod_health_filters_by_service_name(self):
        self.kubernetes_agent.get_pod_health.return_value = {
            "unhealthy_pods": [{"name": "checkout-abc"}, {"name": "cart-xyz"}],
            "high_restart_pods": [{"name": "checkout-abc"}, {"name": "cart-xyz"}],
        }
        result = self._invoke("get_pod_health", service_name="checkout")
        self.assertEqual(result["unhealthy_pods"], [{"name": "checkout-abc"}])
        self.assertEqual(result["high_restart_pods"], [{"name": "checkout-abc"}])

    def test_get_deployment_health_filters_by_service_name(self):
        self.kubernetes_agent.get_unhealthy_deployments.return_value = [
            {"name": "checkout", "healthy": False},
            {"name": "cart", "healthy": False},
        ]
        result = self._invoke("get_deployment_health", service_name="checkout")
        self.assertEqual(result, [{"name": "checkout", "healthy": False}])

    def test_get_recent_k8s_events_filters_by_service_name(self):
        self.kubernetes_agent.get_warning_events.return_value = [
            {"object": "Pod/checkout-abc", "reason": "BackOff"},
            {"object": "Pod/cart-xyz", "reason": "BackOff"},
        ]
        result = self._invoke("get_recent_k8s_events", service_name="checkout")
        self.assertEqual(len(result), 1)
        self.assertIn("checkout", result[0]["object"])

    def test_get_pod_logs_delegates_to_kubernetes_agent_with_namespace(self):
        self.kubernetes_agent.get_pod_logs.return_value = "log line 1\nlog line 2"
        result = self.tool_map["get_pod_logs"].invoke({"pod_name": "checkout-abc", "tail_lines": 50})
        self.kubernetes_agent.get_pod_logs.assert_called_once_with(
            "otel-demo", "checkout-abc", tail_lines=50
        )
        self.assertIn("log line 1", result)

    def test_get_pod_logs_handles_empty_output_without_returning_blank_string(self):
        self.kubernetes_agent.get_pod_logs.return_value = ""
        result = self.tool_map["get_pod_logs"].invoke({"pod_name": "checkout-abc"})
        self.assertTrue(result)  # non-empty placeholder, not ""

    def test_get_active_alerts_delegates_to_alert_agent(self):
        self.alert_agent.get_active_alerts.return_value = [{"name": "CheckoutHighErrors"}]
        result = self._invoke("get_active_alerts")
        self.assertEqual(result[0]["name"], "CheckoutHighErrors")

    def test_get_critical_alerts_delegates_to_alert_agent(self):
        self.alert_agent.get_critical_alerts.return_value = [{"name": "CheckoutDown", "severity": "critical"}]
        result = self._invoke("get_critical_alerts")
        self.assertEqual(result[0]["severity"], "critical")

    def test_get_incident_summary_delegates_to_alert_agent(self):
        self.alert_agent.get_incident_summary.return_value = {"total_active": 2, "critical_count": 1}
        result = self._invoke("get_incident_summary")
        self.assertEqual(result["total_active"], 2)

    def test_get_correlated_incidents_delegates_to_alert_correlator(self):
        self.alert_correlator.get_correlated_incidents.return_value = [
            {"service_name": "checkout", "alert_count": 2, "max_severity": "critical"}
        ]
        result = self._invoke("get_correlated_incidents")
        self.assertEqual(result[0]["service_name"], "checkout")
        self.alert_correlator.get_correlated_incidents.assert_called_once()

    def test_get_incident_history_returns_matching_records(self):
        self.incident_store.create("fp1", alert_name="CheckoutHighErrors", service="checkout", severity="critical")
        self.incident_store.update("fp1", status="completed", content="RCA: OOMKilled")
        self.incident_store.create("fp2", alert_name="FrontendPodDown", service="frontend", severity="warning")

        result = self._invoke("get_incident_history", service_name="checkout")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["fingerprint"], "fp1")
        self.assertEqual(result[0]["content"], "RCA: OOMKilled")

    def test_get_incident_history_defaults_to_all_services(self):
        self.incident_store.create("fp1", alert_name="CheckoutHighErrors", service="checkout", severity="critical")
        self.incident_store.create("fp2", alert_name="FrontendPodDown", service="frontend", severity="warning")

        result = self._invoke("get_incident_history")

        self.assertEqual({r["fingerprint"] for r in result}, {"fp1", "fp2"})

    def test_get_incident_history_with_no_matches_returns_empty_list(self):
        result = self._invoke("get_incident_history", service_name="checkout")

        self.assertEqual(result, [])

    def test_search_similar_incidents_finds_related_past_incident(self):
        self.incident_store.create("fp1", alert_name="ProductCatalogPodDown", service="product-catalog", severity="critical")
        self.incident_store.update("fp1", content="Root cause: product-catalog was scaled to 0 replicas.")
        self.incident_store.create("fp2", alert_name="CheckoutHighLatency", service="checkout", severity="warning")
        self.incident_store.update("fp2", content="Root cause: slow downstream payment call during a traffic burst.")

        result = self._invoke("search_similar_incidents", query="product-catalog scaled to zero replicas")

        self.assertGreaterEqual(len(result), 1)
        self.assertEqual(result[0]["fingerprint"], "fp1")
        self.assertIn("similarity", result[0])

    def test_search_similar_incidents_with_no_matches_returns_empty_list(self):
        result = self._invoke("search_similar_incidents", query="anything at all")

        self.assertEqual(result, [])

    def test_detect_service_anomalies_delegates_to_anomaly_detector(self):
        self.anomaly_detector.scan_service.return_value = {
            "service_name": "checkout", "has_anomaly": True, "checks": {},
        }
        result = self._invoke("detect_service_anomalies", service_name="checkout", lookback_minutes=30)
        self.anomaly_detector.scan_service.assert_called_once_with("checkout", 30)
        self.assertTrue(result["has_anomaly"])

    def test_investigate_service_builds_synthetic_alert_and_returns_report(self):
        self.investigation_graph.investigate.return_value = {"rca_report": "# Root Cause Analysis Report\n..."}
        result = self.tool_map["investigate_service"].invoke({"service_name": "checkout"})

        called_alert = self.investigation_graph.investigate.call_args[0][0]
        self.assertEqual(called_alert["name"], "ManualInvestigation")
        self.assertEqual(called_alert["severity"], "critical")
        self.assertEqual(called_alert["labels"]["service_name"], "checkout")
        self.assertIn("Root Cause Analysis Report", result)

    def test_investigate_service_honors_explicit_alert_name_and_severity(self):
        self.investigation_graph.investigate.return_value = {"rca_report": "report"}
        self.tool_map["investigate_service"].invoke(
            {"service_name": "checkout", "alert_name": "CheckoutHighErrors", "severity": "high"}
        )
        called_alert = self.investigation_graph.investigate.call_args[0][0]
        self.assertEqual(called_alert["name"], "CheckoutHighErrors")
        self.assertEqual(called_alert["severity"], "high")

    def test_investigate_service_handles_missing_report_key(self):
        self.investigation_graph.investigate.return_value = {}
        result = self.tool_map["investigate_service"].invoke({"service_name": "checkout"})
        self.assertEqual(result, "(no report generated)")

    def test_generate_runbook_builds_synthetic_alert_saves_file_and_returns_report(self):
        self.investigation_graph.generate_runbook.return_value = {
            "service_name": "product-catalog",
            "runbook_report": "# Recover product-catalog from 0 replicas\n\n## Remediation Steps\n1. kubectl scale...",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("copilot_tools.RUNBOOKS_DIR", tmpdir):
                result = self.tool_map["generate_runbook"].invoke({"service_name": "product-catalog"})

            called_alert = self.investigation_graph.generate_runbook.call_args[0][0]
            self.assertEqual(called_alert["name"], "ManualInvestigation")
            self.assertEqual(called_alert["labels"]["service_name"], "product-catalog")
            self.assertIn("Recover product-catalog from 0 replicas", result)
            self.assertIn("Saved to", result)

            saved_files = os.listdir(tmpdir)
            self.assertEqual(len(saved_files), 1)
            self.assertTrue(saved_files[0].startswith("product-catalog_"))
            with open(os.path.join(tmpdir, saved_files[0])) as f:
                self.assertIn("Recover product-catalog from 0 replicas", f.read())

    def test_generate_runbook_honors_explicit_alert_name_and_severity(self):
        self.investigation_graph.generate_runbook.return_value = {
            "service_name": "product-catalog", "runbook_report": "# report",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("copilot_tools.RUNBOOKS_DIR", tmpdir):
                self.tool_map["generate_runbook"].invoke(
                    {"service_name": "product-catalog", "alert_name": "ProductCatalogPodDown", "severity": "critical"}
                )
        called_alert = self.investigation_graph.generate_runbook.call_args[0][0]
        self.assertEqual(called_alert["name"], "ProductCatalogPodDown")

    def test_generate_runbook_handles_missing_report_without_writing_a_file(self):
        self.investigation_graph.generate_runbook.return_value = {"service_name": "product-catalog"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("copilot_tools.RUNBOOKS_DIR", tmpdir):
                result = self.tool_map["generate_runbook"].invoke({"service_name": "product-catalog"})
            self.assertIn("Could not generate a runbook", result)
            self.assertEqual(os.listdir(tmpdir), [])

    def test_empty_list_results_serialize_to_a_non_empty_json_string(self):
        # Regression test: LangChain's tool-output normalization treats a raw
        # empty list `[]` as already-valid message content (vacuous `all()`
        # over an empty sequence) instead of stringifying it, producing
        # ToolMessage(content=[]) — which providers like Groq reject outright
        # ("minimum number of items is 1"). Every list/dict-returning tool
        # must come back as a `str` (never a bare list), even when empty.
        self.kubernetes_agent.get_warning_events.return_value = []
        self.alert_agent.get_active_alerts.return_value = []
        self.metrics_agent.get_top_cpu_consumers.return_value = []

        for name, kwargs in [
            ("get_recent_k8s_events", {"service_name": "frontend-proxy"}),
            ("get_active_alerts", {}),
            ("get_top_cpu_consumers", {}),
        ]:
            raw = self.tool_map[name].invoke(kwargs)
            self.assertIsInstance(raw, str)
            self.assertTrue(raw)  # never an empty string either
            self.assertEqual(json.loads(raw), [])
