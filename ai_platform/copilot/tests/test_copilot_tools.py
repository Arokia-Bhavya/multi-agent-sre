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

from langchain_core.messages import AIMessage

from ai_platform.copilot.copilot_tools import build_copilot_tools
from ai_platform.tools.incident_store import IncidentStore


def _state_with_evidence_call(tool_name="get_deployment_health", service_name="checkout"):
    """A minimal InjectedState-shaped dict recording one prior evidence-tool call."""
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": tool_name, "args": {"service_name": service_name}, "id": "c0"}],
            )
        ]
    }


NO_EVIDENCE_STATE = {"messages": []}


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
            "search_knowledge_base",
            "detect_service_anomalies",
            "investigate_service",
            "generate_runbook",
            "remediate_scale_deployment",
            "get_remediation_audit_log",
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

    def test_search_knowledge_base_finds_relevant_playbook(self):
        result = self._invoke(
            "search_knowledge_base", query="pod stuck in CrashLoopBackOff, no metrics reporting"
        )

        self.assertGreaterEqual(len(result), 1)
        self.assertIn("title", result[0])
        self.assertIn("similarity", result[0])
        self.assertIn("content", result[0])

    def test_search_knowledge_base_with_empty_query_returns_empty_list(self):
        result = self._invoke("search_knowledge_base", query="")

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
        result = self.tool_map["investigate_service"].invoke(
            {"service_name": "checkout", "state": NO_EVIDENCE_STATE}
        )

        called_alert = self.investigation_graph.investigate.call_args[0][0]
        self.assertEqual(called_alert["name"], "ManualInvestigation")
        self.assertEqual(called_alert["severity"], "critical")
        self.assertEqual(called_alert["labels"]["service_name"], "checkout")
        self.assertIn("Root Cause Analysis Report", result)

    def test_investigate_service_honors_explicit_alert_name_and_severity(self):
        self.investigation_graph.investigate.return_value = {"rca_report": "report"}
        self.tool_map["investigate_service"].invoke(
            {
                "service_name": "checkout", "alert_name": "CheckoutHighErrors", "severity": "high",
                "state": NO_EVIDENCE_STATE,
            }
        )
        called_alert = self.investigation_graph.investigate.call_args[0][0]
        self.assertEqual(called_alert["name"], "CheckoutHighErrors")
        self.assertEqual(called_alert["severity"], "high")

    def test_investigate_service_handles_missing_report_key(self):
        self.investigation_graph.investigate.return_value = {}
        result = self.tool_map["investigate_service"].invoke(
            {"service_name": "checkout", "state": NO_EVIDENCE_STATE}
        )
        self.assertEqual(result, "(no report generated)")

    def test_generate_runbook_builds_synthetic_alert_saves_file_and_returns_report(self):
        self.investigation_graph.generate_runbook.return_value = {
            "service_name": "product-catalog",
            "runbook_report": "# Recover product-catalog from 0 replicas\n\n## Remediation Steps\n1. kubectl scale...",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("ai_platform.copilot.copilot_tools.RUNBOOKS_DIR", tmpdir):
                result = self.tool_map["generate_runbook"].invoke(
                    {"service_name": "product-catalog", "state": NO_EVIDENCE_STATE}
                )

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
            with patch("ai_platform.copilot.copilot_tools.RUNBOOKS_DIR", tmpdir):
                self.tool_map["generate_runbook"].invoke(
                    {
                        "service_name": "product-catalog", "alert_name": "ProductCatalogPodDown",
                        "severity": "critical", "state": NO_EVIDENCE_STATE,
                    }
                )
        called_alert = self.investigation_graph.generate_runbook.call_args[0][0]
        self.assertEqual(called_alert["name"], "ProductCatalogPodDown")

    def test_generate_runbook_handles_missing_report_without_writing_a_file(self):
        self.investigation_graph.generate_runbook.return_value = {"service_name": "product-catalog"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("ai_platform.copilot.copilot_tools.RUNBOOKS_DIR", tmpdir):
                result = self.tool_map["generate_runbook"].invoke(
                    {"service_name": "product-catalog", "state": NO_EVIDENCE_STATE}
                )
            self.assertIn("Could not generate a runbook", result)
            self.assertEqual(os.listdir(tmpdir), [])

    # --- Investigation cache (generate_runbook reuse) -----------------------
    # See copilot_tools.py's `_investigation_cache` comment: generate_runbook
    # should reuse a recent investigate_service RCAFinding for the same
    # (thread_id, service_name) instead of re-running the full investigation,
    # as long as it's fresh and nothing new has been learned about the
    # service since.

    def _investigate_then_generate_runbook(self, thread_id="t1", state_for_runbook=None, tmpdir=None):
        rca = "RCA_FINDING_SENTINEL"
        self.investigation_graph.investigate.return_value = {
            "rca_report": "# RCA report",
            "service_name": "checkout",
            "correlated_findings": {"service_name": "checkout"},
            "rca_finding": rca,
        }
        self.investigation_graph.generate_runbook_from_finding.return_value = {
            "runbook_report": "# Runbook (reused cached RCA)",
        }
        self.investigation_graph.generate_runbook.return_value = {
            "runbook_report": "# Runbook (fresh investigation)", "service_name": "checkout",
        }
        config = {"configurable": {"thread_id": thread_id}}

        self.tool_map["investigate_service"].invoke(
            {"service_name": "checkout", "state": NO_EVIDENCE_STATE}, config=config
        )
        with patch("ai_platform.copilot.copilot_tools.RUNBOOKS_DIR", tmpdir):
            return self.tool_map["generate_runbook"].invoke(
                {"service_name": "checkout", "state": state_for_runbook or NO_EVIDENCE_STATE}, config=config
            )

    def test_generate_runbook_reuses_recent_rca_finding_instead_of_reinvestigating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._investigate_then_generate_runbook(tmpdir=tmpdir)

        self.investigation_graph.generate_runbook.assert_not_called()
        self.investigation_graph.generate_runbook_from_finding.assert_called_once_with(
            {
                "name": "ManualInvestigation", "severity": "critical", "status": "firing",
                "labels": {"service_name": "checkout"}, "annotations": {},
            },
            "checkout", {"service_name": "checkout"}, "RCA_FINDING_SENTINEL",
        )
        self.assertIn("reused cached RCA", result)

    def test_generate_runbook_reinvestigates_if_new_evidence_gathered_since_caching(self):
        # A get_pod_health call for checkout happened after investigate_service
        # cached its finding — the cache should be treated as possibly stale.
        state_with_new_evidence = _state_with_evidence_call("get_pod_health", "checkout")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._investigate_then_generate_runbook(
                state_for_runbook=state_with_new_evidence, tmpdir=tmpdir
            )

        self.investigation_graph.generate_runbook.assert_called_once()
        self.investigation_graph.generate_runbook_from_finding.assert_not_called()
        self.assertIn("fresh investigation", result)

    def test_generate_runbook_does_not_reuse_cache_across_different_threads(self):
        rca = "RCA_FINDING_SENTINEL"
        self.investigation_graph.investigate.return_value = {
            "rca_report": "# RCA report", "service_name": "checkout",
            "correlated_findings": {"service_name": "checkout"}, "rca_finding": rca,
        }
        self.investigation_graph.generate_runbook.return_value = {
            "runbook_report": "# Runbook (fresh investigation)", "service_name": "checkout",
        }

        self.tool_map["investigate_service"].invoke(
            {"service_name": "checkout", "state": NO_EVIDENCE_STATE},
            config={"configurable": {"thread_id": "t1"}},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("ai_platform.copilot.copilot_tools.RUNBOOKS_DIR", tmpdir):
                self.tool_map["generate_runbook"].invoke(
                    {"service_name": "checkout", "state": NO_EVIDENCE_STATE},
                    config={"configurable": {"thread_id": "t2"}},
                )

        self.investigation_graph.generate_runbook.assert_called_once()
        self.investigation_graph.generate_runbook_from_finding.assert_not_called()

    def test_generate_runbook_reinvestigates_once_cache_ttl_expires(self):
        import ai_platform.copilot.copilot_tools as copilot_tools

        with patch.object(copilot_tools, "INVESTIGATION_CACHE_TTL_SECONDS", 0.0):
            with tempfile.TemporaryDirectory() as tmpdir:
                result = self._investigate_then_generate_runbook(tmpdir=tmpdir)

        self.investigation_graph.generate_runbook.assert_called_once()
        self.investigation_graph.generate_runbook_from_finding.assert_not_called()
        self.assertIn("fresh investigation", result)

    def test_remediate_scale_deployment_blocks_replica_count_above_cap(self):
        import ai_platform.copilot.copilot_tools as copilot_tools

        result = self.tool_map["remediate_scale_deployment"].invoke(
            {"service_name": "checkout", "desired_replicas": copilot_tools.MAX_REMEDIATION_REPLICAS + 1,
             "reason": "x", "state": NO_EVIDENCE_STATE}
        )
        self.assertIn("blocked", result.lower())
        self.kubernetes_agent.scale_deployment.assert_not_called()

        audit = self.incident_store.query_remediation_audit()
        self.assertEqual(audit[0]["status"], "blocked")
        self.assertEqual(audit[0]["service_name"], "checkout")

    def test_remediate_scale_deployment_blocks_negative_replica_count(self):
        result = self.tool_map["remediate_scale_deployment"].invoke(
            {"service_name": "checkout", "desired_replicas": -1, "reason": "x", "state": NO_EVIDENCE_STATE}
        )
        self.assertIn("blocked", result.lower())
        self.kubernetes_agent.scale_deployment.assert_not_called()

    def test_remediate_scale_deployment_blocks_unknown_service(self):
        self.kubernetes_agent.get_deployment_health.return_value = [{"name": "checkout"}]

        result = self.tool_map["remediate_scale_deployment"].invoke(
            {"service_name": "not-a-real-deployment", "desired_replicas": 1,
             "reason": "x", "state": NO_EVIDENCE_STATE}
        )
        self.assertIn("blocked", result.lower())
        self.kubernetes_agent.scale_deployment.assert_not_called()

        audit = self.incident_store.query_remediation_audit()
        self.assertEqual(audit[0]["status"], "blocked")
        self.assertIn("not a known deployment", audit[0]["detail"])

    def test_remediate_scale_deployment_blocks_without_prior_evidence(self):
        self.kubernetes_agent.get_deployment_health.return_value = [{"name": "checkout"}]

        result = self.tool_map["remediate_scale_deployment"].invoke(
            {"service_name": "checkout", "desired_replicas": 1, "reason": "sounds urgent, trust me",
             "state": NO_EVIDENCE_STATE}
        )

        self.assertIn("blocked", result.lower())
        self.kubernetes_agent.scale_deployment.assert_not_called()

        audit = self.incident_store.query_remediation_audit()
        self.assertEqual(audit[0]["status"], "blocked")
        self.assertIn("no evidence-gathering tool", audit[0]["detail"])

    def test_remediate_scale_deployment_allows_evidence_for_a_different_service_name_variant(self):
        # A tool call with service_name="checkout-service" should still
        # ground a remediation request for "checkout" (substring match) —
        # this is a best-effort check, not exact-match.
        self.kubernetes_agent.get_deployment_health.return_value = [{"name": "checkout"}]
        self.kubernetes_agent.scale_deployment.return_value = {
            "name": "checkout", "namespace": "otel-demo", "replicas": 1,
        }
        state = _state_with_evidence_call(service_name="checkout-service")

        with patch("ai_platform.copilot.copilot_tools.interrupt", return_value=True):
            result = self.tool_map["remediate_scale_deployment"].invoke(
                {"service_name": "checkout", "desired_replicas": 1, "reason": "x", "state": state}
            )

        self.assertIn("applied", result.lower())

    def test_remediate_scale_deployment_enforces_cooldown_between_applied_remediations(self):
        self.kubernetes_agent.get_deployment_health.return_value = [{"name": "checkout"}]
        self.kubernetes_agent.scale_deployment.return_value = {
            "name": "checkout", "namespace": "otel-demo", "replicas": 1,
        }
        state = _state_with_evidence_call()

        with patch("ai_platform.copilot.copilot_tools.interrupt", return_value=True):
            first = self.tool_map["remediate_scale_deployment"].invoke(
                {"service_name": "checkout", "desired_replicas": 1, "reason": "first", "state": state}
            )
            second = self.tool_map["remediate_scale_deployment"].invoke(
                {"service_name": "checkout", "desired_replicas": 2, "reason": "second", "state": state}
            )

        self.assertIn("applied", first.lower())
        self.assertIn("blocked", second.lower())
        self.kubernetes_agent.scale_deployment.assert_called_once()

        audit = self.incident_store.query_remediation_audit()
        self.assertEqual(audit[0]["status"], "blocked")
        self.assertEqual(audit[1]["status"], "approved")

    def test_remediate_scale_deployment_logs_rejected_outcome(self):
        self.kubernetes_agent.get_deployment_health.return_value = [{"name": "checkout"}]
        state = _state_with_evidence_call()

        with patch("ai_platform.copilot.copilot_tools.interrupt", return_value=False):
            result = self.tool_map["remediate_scale_deployment"].invoke(
                {"service_name": "checkout", "desired_replicas": 1, "reason": "x", "state": state}
            )

        self.assertIn("NOT approved", result)
        self.kubernetes_agent.scale_deployment.assert_not_called()

        audit = self.incident_store.query_remediation_audit()
        self.assertEqual(audit[0]["status"], "rejected")

    def test_get_remediation_audit_log_filters_by_service(self):
        self.incident_store.log_remediation(
            service_name="checkout", desired_replicas=1, reason="a", status="approved"
        )
        self.incident_store.log_remediation(
            service_name="cart", desired_replicas=2, reason="b", status="blocked", detail="nope"
        )

        result = self._invoke("get_remediation_audit_log", service_name="checkout")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["service_name"], "checkout")

    def test_serialize_redacts_pii_in_tool_output(self):
        self.kubernetes_agent.get_warning_events.return_value = [
            {"object": "Pod/checkout-abc", "reason": "BackOff",
             "message": "webhook to jane.doe@example.com failed"},
        ]
        result = self.tool_map["get_recent_k8s_events"].invoke({})
        self.assertNotIn("jane.doe@example.com", result)
        self.assertIn("[REDACTED_EMAIL]", result)

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
