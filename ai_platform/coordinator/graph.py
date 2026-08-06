"""
LangGraph coordinator for the Multi-Agent SRE Platform (Milestone 4).

Workflow:

    receive_alert
        -> invoke_metrics_agent     \\
        -> invoke_trace_agent        }-- run in parallel
        -> invoke_kubernetes_agent  /
        -> correlate_findings   (deterministic data-shaping, no LLM)
        -> produce_rca_report   (LLM reasoning, via langchain-anthropic)

The three agent-invocation nodes only depend on `service_name` (resolved by
receive_alert), not on each other, so LangGraph runs them concurrently in the
same super-step before joining at correlate_findings.

produce_rca_report needs a chat model set up for the `LLM_PROVIDER` in the
environment (default "anthropic", needs ANTHROPIC_API_KEY; or "groq", needs
GROQ_API_KEY — Groq has a free tier), unless a custom `llm` is injected
(e.g. for tests).
"""

import os
from typing import Any, Callable, Dict, List, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from ai_platform.coordinator.state import InvestigationState
from ai_platform.coordinator.rca import RCAFinding, RCA_SYSTEM_PROMPT, build_rca_prompt, render_markdown_report
from ai_platform.coordinator.runbook import (
    RunbookDoc,
    RUNBOOK_SYSTEM_PROMPT,
    build_runbook_prompt,
    render_runbook_markdown,
)

from ai_platform.tools.metrics_agent import MetricsAgent
from ai_platform.tools.trace_agent import TraceAgent
from ai_platform.tools.kubernetes_agent import KubernetesAgent
from ai_platform.tools.service_matching import DEFAULT_KNOWN_SERVICES, resolve_service_from_alert

# Default model per provider. Groq's free tier covers llama-3.3-70b-versatile
# (good quality/tool-calling support) with generous daily limits; swap via
# LLM_MODEL if you want llama-3.1-8b-instant (faster, lighter) instead.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "groq": "llama-3.3-70b-versatile",
}


def extract_service_name(
    alert: Dict[str, Any], known_services: Optional[List[str]] = None
) -> Optional[str]:
    """
    Best-effort extraction of the affected service name from an alert.

    Thin alias for `tools/service_matching.py::resolve_service_from_alert`,
    kept under this name because `auto_responder.py`, `coordinator/__init__.py`,
    and the existing tests all import it from here. See that module for the
    matching rules and for why substring matching was replaced.
    """
    return resolve_service_from_alert(alert, known_services)


class InvestigationGraph:
    """
    Builds and runs the LangGraph coordinator that investigates a firing
    alert end-to-end: gathers metrics/trace/Kubernetes evidence for the
    affected service, correlates it, and asks an LLM to produce a
    structured root-cause report.

    Example:
        graph = InvestigationGraph(
            metrics_agent=MetricsAgent(PrometheusClient("http://localhost:9090")),
            trace_agent=TraceAgent(JaegerClient("http://localhost:16686")),
            kubernetes_agent=KubernetesAgent(KubernetesClient()),
            namespace="otel-demo",
        )
        result = graph.investigate(alert)
        print(result["rca_report"])
    """

    def __init__(
        self,
        metrics_agent: MetricsAgent,
        trace_agent: TraceAgent,
        kubernetes_agent: KubernetesAgent,
        namespace: str = "otel-demo",
        llm: Optional[Any] = None,
        known_services: Optional[List[str]] = None,
    ):
        """
        Args:
            metrics_agent, trace_agent, kubernetes_agent: Milestone 3 agents.
            namespace: Kubernetes namespace to inspect.
            llm: Optional pre-built chat model exposing `.with_structured_output()`.
                If omitted, one is built lazily on first use based on the
                LLM_PROVIDER env var ("anthropic" (default) or "groq"), so
                importing/constructing this class never requires an API key
                unless produce_rca_report actually runs. Inject a fake here
                for tests, or a ChatGroq/ChatAnthropic instance directly to
                skip the env-var lookup.
            known_services: Override the default OTel Demo service name list
                used for alert -> service name matching.
        """
        self.metrics_agent = metrics_agent
        self.trace_agent = trace_agent
        self.kubernetes_agent = kubernetes_agent
        self.namespace = namespace
        self.known_services = known_services or DEFAULT_KNOWN_SERVICES
        self._llm = llm
        self._compiled: CompiledStateGraph = self._build_graph().compile()

    def investigate(self, alert: Dict[str, Any]) -> InvestigationState:
        """
        Run the full investigation workflow for a single alert and return the
        final state (includes `rca_report`, the markdown RCA report).
        """
        initial_state: InvestigationState = {
            "alert": alert,
            "known_services": self.known_services,
            "errors": [],
        }
        return self._compiled.invoke(initial_state)

    def generate_runbook(self, alert: Dict[str, Any]) -> InvestigationState:
        """
        Run the full investigation, then ask the LLM for a second, more
        execution-focused structured output: a step-by-step remediation
        runbook grounded in the RCA finding and evidence, rendered to
        markdown as `runbook_report` (with the structured `RunbookDoc` also
        available as `runbook`).

        This always re-runs the investigation rather than accepting an
        already-computed result, since alert state can change between an
        investigation and a runbook request and a stale runbook is worse
        than a slightly slower one.
        """
        result = self.investigate(alert)
        rca = result.get("rca_finding")
        if rca is None:
            # investigate() itself failed before reaching produce_rca_report
            # (e.g. no service_name resolved) — nothing to build a runbook from.
            return result

        prompt = build_runbook_prompt(
            alert, result.get("service_name"), result["correlated_findings"], rca
        )
        messages = [
            {"role": "system", "content": RUNBOOK_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        runbook = self._invoke_structured(messages, RunbookDoc)
        report = render_runbook_markdown(alert, result.get("service_name"), rca, runbook)
        return {**result, "runbook": runbook, "runbook_report": report}

    @property
    def llm(self) -> Any:
        if self._llm is None:
            provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
            if provider not in DEFAULT_MODELS:
                raise ValueError(
                    f"Unknown LLM_PROVIDER '{provider}'; expected one of {list(DEFAULT_MODELS)}"
                )
            model = os.getenv("LLM_MODEL", DEFAULT_MODELS[provider])

            if provider == "groq":
                from langchain_groq import ChatGroq
                self._llm = ChatGroq(model=model, temperature=0)
            else:
                from langchain_anthropic import ChatAnthropic
                self._llm = ChatAnthropic(model=model)
        return self._llm

    def _build_graph(self) -> StateGraph:
        workflow = StateGraph(InvestigationState)
        workflow.add_node("receive_alert", self._receive_alert)
        workflow.add_node("invoke_metrics_agent", self._invoke_metrics_agent)
        workflow.add_node("invoke_trace_agent", self._invoke_trace_agent)
        workflow.add_node("invoke_kubernetes_agent", self._invoke_kubernetes_agent)
        workflow.add_node("correlate_findings", self._correlate_findings)
        workflow.add_node("produce_rca_report", self._produce_rca_report)

        workflow.add_edge(START, "receive_alert")
        workflow.add_edge("receive_alert", "invoke_metrics_agent")
        workflow.add_edge("receive_alert", "invoke_trace_agent")
        workflow.add_edge("receive_alert", "invoke_kubernetes_agent")
        workflow.add_edge("invoke_metrics_agent", "correlate_findings")
        workflow.add_edge("invoke_trace_agent", "correlate_findings")
        workflow.add_edge("invoke_kubernetes_agent", "correlate_findings")
        workflow.add_edge("correlate_findings", "produce_rca_report")
        workflow.add_edge("produce_rca_report", END)
        return workflow

    # ---- Node implementations -------------------------------------------------

    def _receive_alert(self, state: InvestigationState) -> Dict[str, Any]:
        """Step 1: Receive alert — validate it and resolve the affected service."""
        alert = state["alert"]
        known_services = state.get("known_services") or self.known_services
        service_name = extract_service_name(alert, known_services)

        errors = list(state.get("errors", []))
        if not service_name:
            errors.append(f"Could not determine affected service from alert '{alert.get('name')}'")

        return {"service_name": service_name, "errors": errors}

    def _invoke_metrics_agent(self, state: InvestigationState) -> Dict[str, Any]:
        """Step 2: Invoke Metrics Agent."""
        service_name = state.get("service_name")
        if not service_name:
            return {"metrics_findings": {}}
        try:
            return {"metrics_findings": self.metrics_agent.get_service_summary(service_name)}
        except Exception as e:
            return {"metrics_findings": {"error": f"metrics agent failed: {e}"}}

    def _invoke_trace_agent(self, state: InvestigationState) -> Dict[str, Any]:
        """Step 3: Invoke Trace Agent."""
        service_name = state.get("service_name")
        if not service_name:
            return {"trace_findings": {}}
        try:
            slow_spans = self.trace_agent.get_slow_spans(service_name, min_duration_ms=100)
            critical_path: List[Dict[str, Any]] = []
            if slow_spans:
                summary = self.trace_agent.get_trace_summary(slow_spans[0]["trace_id"])
                critical_path = summary.get("critical_path", [])
            return {"trace_findings": {"slow_spans": slow_spans, "critical_path": critical_path}}
        except Exception as e:
            return {"trace_findings": {"error": f"trace agent failed: {e}"}}

    def _invoke_kubernetes_agent(self, state: InvestigationState) -> Dict[str, Any]:
        """Step 4: Invoke Kubernetes Agent."""
        service_name = state.get("service_name")
        try:
            pod_health = self.kubernetes_agent.get_pod_health(self.namespace)
            unhealthy_pods = pod_health["unhealthy_pods"]
            high_restart_pods = pod_health["high_restart_pods"]
            unhealthy_deployments = self.kubernetes_agent.get_unhealthy_deployments(self.namespace)
            warning_events = self.kubernetes_agent.get_warning_events(self.namespace)

            def matches(name: str) -> bool:
                return bool(service_name) and service_name in (name or "")

            return {
                "kubernetes_findings": {
                    "unhealthy_pods": [p for p in unhealthy_pods if matches(p.get("name", ""))],
                    "high_restart_pods": [p for p in high_restart_pods if matches(p.get("name", ""))],
                    "unhealthy_deployments": [
                        d for d in unhealthy_deployments if matches(d.get("name", ""))
                    ],
                    "warning_events": [e for e in warning_events if matches(e.get("object", ""))],
                }
            }
        except Exception as e:
            return {"kubernetes_findings": {"error": f"kubernetes agent failed: {e}"}}

    def _correlate_findings(self, state: InvestigationState) -> Dict[str, Any]:
        """
        Step 5: Correlate findings — deterministic aggregation/trimming of the
        three agents' raw output into a compact evidence bundle. No LLM call
        happens here; reasoning is deferred to produce_rca_report.
        """
        metrics = state.get("metrics_findings", {}) or {}
        traces = state.get("trace_findings", {}) or {}
        k8s = state.get("kubernetes_findings", {}) or {}

        data_gaps = [
            findings["error"]
            for findings in (metrics, traces, k8s)
            if isinstance(findings, dict) and "error" in findings
        ]

        correlated = {
            "service_name": state.get("service_name"),
            "metrics": metrics,
            "slow_spans": traces.get("slow_spans", [])[:5],
            "critical_path": traces.get("critical_path", []),
            "unhealthy_pods": k8s.get("unhealthy_pods", []),
            "high_restart_pods": k8s.get("high_restart_pods", []),
            "unhealthy_deployments": k8s.get("unhealthy_deployments", []),
            "warning_events": k8s.get("warning_events", [])[:5],
            "data_gaps": data_gaps + state.get("errors", []),
        }
        return {"correlated_findings": correlated}

    def _produce_rca_report(self, state: InvestigationState) -> Dict[str, Any]:
        """
        Step 6: Produce RCA report — an LLM reads the correlated findings and
        returns a structured root-cause finding, which is rendered to markdown.
        """
        prompt = build_rca_prompt(state["alert"], state["correlated_findings"])
        messages = [
            {"role": "system", "content": RCA_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        rca = self._invoke_structured(messages, RCAFinding)
        report = render_markdown_report(
            state["alert"], state.get("service_name"), state["correlated_findings"], rca
        )
        return {"rca_finding": rca, "rca_report": report}

    def _invoke_structured(
        self, messages: List[Dict[str, str]], output_model: type, attempts: int = 3
    ) -> Any:
        """
        Invoke the LLM for a structured output (RCAFinding or RunbookDoc),
        retrying on failure.

        Groq's Llama tool-calling occasionally emits a malformed function call
        (surfaces as `groq.BadRequestError: ... tool_use_failed`) instead of a
        proper tool_calls response — the content is usually fine, just wrapped
        wrong. Retry the default (function-calling) method a few times first
        since it's non-deterministic, then fall back to `json_mode`, which is
        more reliable for Llama-family models on Groq.
        """
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                structured_llm = self.llm.with_structured_output(output_model)
                return structured_llm.invoke(messages)
            except Exception as exc:  # e.g. groq.BadRequestError (tool_use_failed)
                last_error = exc

        try:
            structured_llm = self.llm.with_structured_output(output_model, method="json_mode")
            json_messages = messages + [
                {
                    "role": "user",
                    "content": (
                        "Respond with a single valid JSON object matching the "
                        "required schema. No prose, no markdown fences."
                    ),
                }
            ]
            return structured_llm.invoke(json_messages)
        except Exception:
            raise last_error from None
