"""
Unit tests for the Milestone 4 LangGraph coordinator.

No cluster, network, or ANTHROPIC_API_KEY is required: the agents are
replaced with MagicMocks and the LLM is replaced with a fake that returns a
canned RCAFinding.
"""

import os
from unittest import TestCase, mock
from unittest.mock import MagicMock

from ai_platform.coordinator.graph import InvestigationGraph, extract_service_name, DEFAULT_KNOWN_SERVICES, DEFAULT_MODELS
from ai_platform.coordinator.rca import RCAFinding
from ai_platform.coordinator.runbook import RunbookDoc


class LLMProviderSelectionTests(TestCase):
    """`InvestigationGraph.llm` lazily builds a chat model based on LLM_PROVIDER,
    so switching to Groq's free tier is just an env var away."""

    def _graph(self):
        return InvestigationGraph(
            metrics_agent=MagicMock(),
            trace_agent=MagicMock(),
            kubernetes_agent=MagicMock(),
        )

    def test_defaults_to_anthropic(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("langchain_anthropic.ChatAnthropic") as mock_chat:
                self._graph().llm
                mock_chat.assert_called_once_with(model=DEFAULT_MODELS["anthropic"])

    def test_llm_provider_groq_builds_chat_groq_with_default_model(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "groq"}, clear=True):
            with mock.patch("langchain_groq.ChatGroq") as mock_chat:
                self._graph().llm
                mock_chat.assert_called_once_with(model=DEFAULT_MODELS["groq"], temperature=0)

    def test_llm_model_env_var_overrides_default(self):
        with mock.patch.dict(
            os.environ, {"LLM_PROVIDER": "groq", "LLM_MODEL": "llama-3.1-8b-instant"}, clear=True
        ):
            with mock.patch("langchain_groq.ChatGroq") as mock_chat:
                self._graph().llm
                mock_chat.assert_called_once_with(model="llama-3.1-8b-instant", temperature=0)

    def test_unknown_provider_raises(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "bogus"}, clear=True):
            with self.assertRaises(ValueError):
                self._graph().llm

    def test_injected_llm_skips_provider_lookup_entirely(self):
        fake = object()
        graph = InvestigationGraph(
            metrics_agent=MagicMock(), trace_agent=MagicMock(), kubernetes_agent=MagicMock(), llm=fake
        )
        self.assertIs(graph.llm, fake)


class ExtractServiceNameTests(TestCase):
    def test_prefers_explicit_service_label(self):
        alert = {"name": "AnythingAtAll", "labels": {"service_name": "checkout"}}
        self.assertEqual(extract_service_name(alert), "checkout")

    def test_falls_back_to_job_label(self):
        alert = {"name": "AnythingAtAll", "labels": {"job": "cart"}}
        self.assertEqual(extract_service_name(alert), "cart")

    def test_matches_longer_service_name_first(self):
        alert = {"name": "FrontendProxyHighLatency", "labels": {}}
        self.assertEqual(extract_service_name(alert), "frontend-proxy")

    def test_matches_shorter_service_name_when_no_longer_match(self):
        alert = {"name": "FrontendHighErrorRate", "labels": {}}
        self.assertEqual(extract_service_name(alert), "frontend")

    def test_returns_none_when_no_match(self):
        alert = {"name": "SomeUnrelatedAlert", "labels": {}}
        self.assertIsNone(extract_service_name(alert, DEFAULT_KNOWN_SERVICES))


class FakeStructuredLLM:
    def __init__(self, result):
        self.result = result
        self.last_messages = None

    def invoke(self, messages):
        self.last_messages = messages
        return self.result


class FakeLLM:
    """
    `result` is either a single value (returned regardless of the requested
    schema — the original single-output-type usage), or a dict mapping
    output model class -> value, for tests that need the LLM to return a
    different structured type per call (e.g. RCAFinding for the RCA step,
    RunbookDoc for the runbook step).
    """

    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def with_structured_output(self, schema):
        value = self.result[schema] if isinstance(self.result, dict) else self.result
        instance = FakeStructuredLLM(value)
        self.calls.append(instance)
        return instance

    @property
    def structured(self):
        """The most recently created structured-output instance (backward-compatible
        with tests written before this fake supported multiple output types)."""
        return self.calls[-1] if self.calls else None


class InvestigationGraphTests(TestCase):
    def setUp(self):
        self.metrics_agent = MagicMock()
        self.trace_agent = MagicMock()
        self.kubernetes_agent = MagicMock()

        self.metrics_agent.get_service_summary.return_value = {
            "service_name": "checkout",
            "request_rate_per_sec": 42.0,
            "error_rate": 0.12,
            "latency_p95_seconds": 1.8,
            "cpu_utilization": 0.9,
            "memory_bytes": 1_000_000,
        }
        self.trace_agent.get_slow_spans.return_value = [
            {"trace_id": "t1", "span_id": "s1", "service": "checkout", "operation": "db", "duration_ms": 900},
        ]
        self.trace_agent.get_trace_summary.return_value = {
            "trace_id": "t1",
            "found": True,
            "span_count": 3,
            "duration_ms": 950,
            "critical_path": [{"service": "checkout", "operation": "db", "duration_ms": 900}],
        }
        self.kubernetes_agent.get_pod_health.return_value = {
            "unhealthy_pods": [
                {"name": "checkout-6f8bcd9c48-b4ss4", "status": "CrashLoopBackOff", "ready": False, "restarts": 7},
                {"name": "cart-9c4df88-xzkvb", "status": "Running", "ready": True, "restarts": 0},
            ],
            "high_restart_pods": [
                {"name": "checkout-6f8bcd9c48-b4ss4", "status": "CrashLoopBackOff", "ready": False, "restarts": 7},
            ],
        }
        self.kubernetes_agent.get_unhealthy_deployments.return_value = []
        self.kubernetes_agent.get_warning_events.return_value = [
            {"type": "Warning", "reason": "BackOff", "object": "Pod/checkout-6f8bcd9c48-b4ss4"},
        ]

        self.fake_rca = RCAFinding(
            likely_root_cause="The checkout pod is crash-looping, causing elevated error rate and latency.",
            confidence="high",
            supporting_evidence=["7 restarts on checkout pod", "12% error rate", "900ms db span"],
            recommended_actions=["Check checkout pod logs", "Roll back last checkout deployment"],
        )
        self.fake_llm = FakeLLM(self.fake_rca)

        self.graph = InvestigationGraph(
            metrics_agent=self.metrics_agent,
            trace_agent=self.trace_agent,
            kubernetes_agent=self.kubernetes_agent,
            namespace="otel-demo",
            llm=self.fake_llm,
        )

    def test_investigate_resolves_service_and_calls_all_agents(self):
        alert = {"name": "CheckoutHighErrors", "severity": "critical", "labels": {"service_name": "checkout"}}
        result = self.graph.investigate(alert)

        self.assertEqual(result["service_name"], "checkout")
        self.metrics_agent.get_service_summary.assert_called_once_with("checkout")
        self.trace_agent.get_slow_spans.assert_called_once_with("checkout", min_duration_ms=100)
        self.trace_agent.get_trace_summary.assert_called_once_with("t1")
        self.kubernetes_agent.get_pod_health.assert_called_once_with("otel-demo")

    def test_kubernetes_findings_are_filtered_to_the_affected_service(self):
        alert = {"name": "CheckoutHighErrors", "labels": {"service_name": "checkout"}}
        result = self.graph.investigate(alert)

        k8s = result["kubernetes_findings"]
        self.assertEqual(len(k8s["unhealthy_pods"]), 1)
        self.assertEqual(k8s["unhealthy_pods"][0]["name"], "checkout-6f8bcd9c48-b4ss4")
        self.assertEqual(len(k8s["high_restart_pods"]), 1)

    def test_correlated_findings_are_capped_and_shaped(self):
        alert = {"name": "CheckoutHighErrors", "labels": {"service_name": "checkout"}}
        result = self.graph.investigate(alert)

        correlated = result["correlated_findings"]
        self.assertEqual(correlated["service_name"], "checkout")
        self.assertEqual(correlated["critical_path"][0]["operation"], "db")
        self.assertEqual(correlated["data_gaps"], [])

    def test_produce_rca_report_calls_llm_and_renders_markdown(self):
        alert = {"name": "CheckoutHighErrors", "severity": "critical", "labels": {"service_name": "checkout"}}
        result = self.graph.investigate(alert)

        self.assertIn("Root Cause Analysis Report", result["rca_report"])
        self.assertIn("crash-looping", result["rca_report"])
        self.assertIn("high", result["rca_report"])
        self.assertIsNotNone(self.fake_llm.structured.last_messages)

    def test_unresolved_service_skips_metrics_and_trace_but_still_reports(self):
        alert = {"name": "SomeUnknownAlert", "labels": {}}
        result = self.graph.investigate(alert)

        self.assertIsNone(result["service_name"])
        self.assertEqual(result["metrics_findings"], {})
        self.assertEqual(result["trace_findings"], {})
        self.metrics_agent.get_service_summary.assert_not_called()
        self.assertIn("Could not determine affected service", result["errors"][0])
        # Kubernetes agent still runs (namespace-wide), just yields no matches.
        self.assertEqual(result["kubernetes_findings"]["unhealthy_pods"], [])

    def test_agent_failure_is_captured_as_a_data_gap_not_a_crash(self):
        self.metrics_agent.get_service_summary.side_effect = RuntimeError("prometheus unreachable")
        alert = {"name": "CheckoutHighErrors", "labels": {"service_name": "checkout"}}
        result = self.graph.investigate(alert)

        self.assertIn("error", result["metrics_findings"])
        self.assertTrue(
            any("metrics agent failed" in gap for gap in result["correlated_findings"]["data_gaps"])
        )
        # The rest of the pipeline still completes.
        self.assertIn("Root Cause Analysis Report", result["rca_report"])


class GenerateRunbookTests(TestCase):
    """Milestone 6: AI-generated remediation runbooks."""

    def setUp(self):
        self.metrics_agent = MagicMock()
        self.trace_agent = MagicMock()
        self.kubernetes_agent = MagicMock()

        self.metrics_agent.get_service_summary.return_value = {
            "service_name": "product-catalog",
            "request_rate_per_sec": 0.0,
            "error_rate": 0.0,
            "latency_p95_seconds": 0.0,
            "cpu_utilization": None,
            "memory_bytes": None,
        }
        self.trace_agent.get_slow_spans.return_value = []
        self.kubernetes_agent.get_pod_health.return_value = {
            "unhealthy_pods": [], "high_restart_pods": [],
        }
        self.kubernetes_agent.get_unhealthy_deployments.return_value = [
            {"name": "product-catalog", "desired_replicas": 0, "ready_replicas": 0, "healthy": False},
        ]
        self.kubernetes_agent.get_warning_events.return_value = []

        self.fake_rca = RCAFinding(
            likely_root_cause="product-catalog was scaled to 0 replicas.",
            confidence="high",
            supporting_evidence=["desired_replicas=0, ready_replicas=0"],
            recommended_actions=["Scale product-catalog back up"],
        )
        self.fake_runbook = RunbookDoc(
            title="Recover product-catalog from 0 replicas",
            summary="product-catalog was scaled to 0; scale it back up and verify.",
            prerequisites=["kubectl access to the otel-demo namespace"],
            steps=["kubectl scale deployment product-catalog --replicas=1 -n otel-demo"],
            verification_steps=["kubectl get pods -n otel-demo | grep product-catalog"],
            rollback_steps=["kubectl scale deployment product-catalog --replicas=0 -n otel-demo"],
        )
        self.fake_llm = FakeLLM({RCAFinding: self.fake_rca, RunbookDoc: self.fake_runbook})

        self.graph = InvestigationGraph(
            metrics_agent=self.metrics_agent,
            trace_agent=self.trace_agent,
            kubernetes_agent=self.kubernetes_agent,
            namespace="otel-demo",
            llm=self.fake_llm,
        )

    def test_generate_runbook_runs_investigation_then_renders_markdown(self):
        alert = {
            "name": "ProductCatalogPodDown", "severity": "critical",
            "labels": {"service_name": "product-catalog"},
        }
        result = self.graph.generate_runbook(alert)

        self.assertEqual(result["service_name"], "product-catalog")
        self.assertIn("Root Cause Analysis Report", result["rca_report"])  # investigate() still ran
        self.assertIn("Recover product-catalog from 0 replicas", result["runbook_report"])
        self.assertIn("kubectl scale deployment product-catalog --replicas=1", result["runbook_report"])
        self.assertIn("## Rollback", result["runbook_report"])
        self.assertEqual(result["runbook"].title, "Recover product-catalog from 0 replicas")

    def test_generate_runbook_grounds_prompt_in_rca_finding(self):
        alert = {
            "name": "ProductCatalogPodDown",
            "labels": {"service_name": "product-catalog"},
        }
        self.graph.generate_runbook(alert)

        runbook_call_messages = self.fake_llm.calls[-1].last_messages
        prompt_text = runbook_call_messages[-1]["content"]
        self.assertIn("product-catalog was scaled to 0 replicas", prompt_text)

    def test_generate_runbook_still_produces_output_with_unresolved_service(self):
        # produce_rca_report runs unconditionally even when receive_alert
        # couldn't resolve a service_name (it just reasons over thinner
        # evidence), so generate_runbook still gets an RCAFinding to build
        # a runbook from — the "rca is None" guard in generate_runbook is
        # defensive for a future graph change, not something this path hits.
        alert = {"name": "SomeUnknownAlert", "labels": {}}
        result = self.graph.generate_runbook(alert)

        self.assertIsNone(result.get("service_name"))
        self.assertIn("runbook_report", result)
