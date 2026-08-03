"""
Copilot tool layer for the Multi-Agent SRE Platform (Milestone 5).

Wraps the Milestone 3 agents (Metrics, Trace, Kubernetes, Alert) and the
Milestone 4 LangGraph coordinator as LangChain tools, so a conversational
agent can call them in response to natural-language questions like
"Why is checkout slow?" or "Which service has the highest CPU usage?".

Each tool is a thin, mostly-deterministic wrapper: it calls exactly one
agent method (or, for `investigate_service`, the full coordinator graph)
and returns plain JSON-serializable data. All reasoning about *what* the
data means is left to the copilot's LLM, not baked into the tool.
"""

import functools
import json
import os
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from langgraph.types import interrupt

from metrics_agent import MetricsAgent
from trace_agent import TraceAgent
from kubernetes_agent import KubernetesAgent
from alert_agent import AlertAgent
from alert_correlator import AlertCorrelator
from anomaly_detector import AnomalyDetector

from graph import InvestigationGraph
from runbook import runbook_filename
from incident_store import IncidentStore
from incident_search import search_similar_incidents as _search_similar_incidents


# Runbooks get saved here so a generated remediation doc is a real file the
# user can open/share, not just chat text that's gone once the session ends.
# Relative to this file (ai-platform/copilot/) rather than cwd, since the
# copilot can be launched from either the repo root or ai-platform/.
RUNBOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "runbooks")


def _serialize(result: Any) -> str:
    """
    Render tool output as a JSON string rather than returning a raw Python
    list/dict.

    This works around a LangChain content-normalization quirk: when a tool
    returns an empty list `[]`, `_normalize_message_content` treats it as an
    already-valid list of message content blocks (an `all(...)` check over an
    empty sequence is vacuously true) instead of falling through to
    stringification, producing `ToolMessage(content=[])`. Groq's chat
    completions API rejects that outright ("'content': minimum number of
    items is 1"), which surfaces as a confusing `groq.BadRequestError` on
    whichever *later* turn happens to send that message back — not a
    transient flake, but a deterministic failure any time a lookup legitimately
    returns zero results (e.g. no matching pods for a service).

    Returning a plain JSON string sidesteps this entirely: it's always a
    `str`, which every provider accepts regardless of how many items are
    inside it.
    """
    return json.dumps(result, default=str)


def _catch_errors(fn):
    """
    Decorator for read-only tool functions: catch any exception raised
    while calling the wrapped agent/client (e.g. a Kubernetes 404 for a
    guessed pod name, a Jaeger lookup failing for a hallucinated trace ID)
    and return it as a normal string result instead of letting it propagate.

    Without this, an exception here escapes the tool call entirely. Whether
    or not the surrounding LangGraph ToolNode's own error handling catches
    it, a bad argument from the model (observed in practice: placeholder
    strings like `trace_id='trace_id_from_get_slow_traces'` or
    `pod_name='pod_name_from_get_pod_health'` when it chains a tool call
    without waiting for the real value from a prior one) has crashed the
    whole turn rather than giving the model a chance to see the error and
    retry with a corrected argument. Applied only to read-only tools —
    `remediate_scale_deployment` is intentionally left to fail loudly since
    it's the one cluster-mutating action.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            return _serialize(
                {"error": f"{type(exc).__name__}: {exc}", "tool": fn.__name__, "args": kwargs or args}
            )

    return wrapper


def build_copilot_tools(
    metrics_agent: MetricsAgent,
    trace_agent: TraceAgent,
    kubernetes_agent: KubernetesAgent,
    alert_agent: AlertAgent,
    investigation_graph: InvestigationGraph,
    namespace: str = "otel-demo",
    alert_correlator: Optional[AlertCorrelator] = None,
    anomaly_detector: Optional[AnomalyDetector] = None,
    incident_store: Optional[IncidentStore] = None,
) -> List[Any]:
    """
    Build the list of LangChain tools the copilot agent can call.

    Args:
        metrics_agent, trace_agent, kubernetes_agent, alert_agent: Milestone 3 agents.
        investigation_graph: Milestone 4 coordinator, used by `investigate_service`
            to run the full metrics + traces + Kubernetes + LLM RCA pipeline.
        namespace: Kubernetes namespace the k8s-related tools inspect.
        alert_correlator: Milestone 6 AlertCorrelator, used by
            `get_correlated_incidents`. Defaults to one built from
            `alert_agent` if omitted.
        anomaly_detector: Milestone 6 AnomalyDetector, used by
            `detect_service_anomalies`. Defaults to one built from
            `metrics_agent`'s Prometheus client if omitted.
        incident_store: Milestone 6 IncidentStore (see
            ai-platform/tools/incident_store.py), used by
            `get_incident_history`. Defaults to a fresh in-memory
            (":memory:") store if omitted — safe for tests, but with no
            history to query; real callers should pass one backed by a
            real file (IncidentStore.DEFAULT_DB_PATH) so there's actual
            history to answer questions from.

    Returns:
        A list of `@tool`-decorated callables suitable for
        `langgraph.prebuilt.create_react_agent(model, tools=...)`.
    """
    alert_correlator = alert_correlator or AlertCorrelator(alert_agent)
    anomaly_detector = anomaly_detector or AnomalyDetector(metrics_agent.prom)
    incident_store = incident_store or IncidentStore()

    @tool
    @_catch_errors
    def get_service_metrics(service_name: str) -> str:
        """
        Get a metrics snapshot for a service: request rate (req/s), error
        rate (fraction of requests returning 5xx), p95 latency (seconds),
        CPU utilization (0-1), and memory usage (bytes).

        Use this to answer questions like "how is checkout performing?" or
        "what's the error rate on frontend?".
        """
        return _serialize(metrics_agent.get_service_summary(service_name))

    @tool
    @_catch_errors
    def get_top_cpu_consumers(limit: int = 5) -> str:
        """
        Get the top N containers by current CPU utilization, across the
        whole cluster. Use this to answer "which service has the highest
        CPU usage?".
        """
        return _serialize(metrics_agent.get_top_cpu_consumers(limit))

    @tool
    @_catch_errors
    def get_latency_by_route(service_name: str, percentile: float = 0.95) -> str:
        """
        Get p{percentile} latency (seconds) broken down by HTTP route for a
        service. Use this to narrow down *which* endpoint is slow within a
        service, e.g. after get_service_metrics shows high overall latency.
        """
        return _serialize(metrics_agent.get_latency_by_route(service_name, percentile))

    @tool
    @_catch_errors
    def get_slow_traces(service_name: str, min_duration_ms: float = 100) -> str:
        """
        Find the slowest recent spans (individual units of work within a
        trace) for a service, sorted slowest-first. Use this to answer
        "why is <service> slow?" — the returned spans' `service` and
        `operation` fields point at where time is being spent.
        """
        return _serialize(trace_agent.get_slow_spans(service_name, min_duration_ms=min_duration_ms))

    @tool
    @_catch_errors
    def get_trace_critical_path(trace_id: str) -> str:
        """
        Get the critical path for a specific trace ID: the ordered chain of
        spans (root first) that actually determines the trace's overall
        latency. Use this after get_slow_traces to see which downstream
        service a slow trace's time is really going to.
        """
        return _serialize(trace_agent.get_critical_path(trace_id))

    @tool
    @_catch_errors
    def get_pod_health(service_name: str = "") -> str:
        """
        Get Kubernetes pod health: unhealthy pods (not Running or failing
        readiness) and pods with a high restart count (>=3, a common
        crash-loop symptom). If `service_name` is given, results are
        filtered to pods whose name contains it; otherwise the whole
        namespace is returned. Use this to answer "why did <service> become
        unavailable?".
        """
        health = kubernetes_agent.get_pod_health(namespace)
        unhealthy = health["unhealthy_pods"]
        high_restart = health["high_restart_pods"]
        if service_name:
            unhealthy = [p for p in unhealthy if service_name in p.get("name", "")]
            high_restart = [p for p in high_restart if service_name in p.get("name", "")]
        return _serialize({"unhealthy_pods": unhealthy, "high_restart_pods": high_restart})

    @tool
    @_catch_errors
    def get_deployment_health(service_name: str = "") -> str:
        """
        Get Kubernetes deployment health (ready vs. desired replica counts).
        If `service_name` is given, results are filtered to deployments
        whose name contains it; otherwise all deployments in the namespace
        are returned.
        """
        deployments = kubernetes_agent.get_unhealthy_deployments(namespace)
        if service_name:
            deployments = [d for d in deployments if service_name in d.get("name", "")]
        return _serialize(deployments)

    @tool
    @_catch_errors
    def get_recent_k8s_events(service_name: str = "", limit: int = 20) -> str:
        """
        Get recent Kubernetes "Warning" events (scheduling failures, probe
        failures, image pull errors, OOMKills, etc.). If `service_name` is
        given, results are filtered to events whose object name contains it.
        """
        events = kubernetes_agent.get_warning_events(namespace, limit=limit * 2 if service_name else limit)
        if service_name:
            events = [e for e in events if service_name in e.get("object", "")][:limit]
        return _serialize(events)

    @tool
    @_catch_errors
    def get_pod_logs(pod_name: str, tail_lines: int = 100) -> str:
        """
        Get recent log lines from a Kubernetes pod. Use this when you need
        to see the actual error message from a crashing or unhealthy pod
        (get its name first from get_pod_health).
        """
        logs = kubernetes_agent.get_pod_logs(namespace, pod_name, tail_lines=tail_lines)
        return logs if logs else "(no log output)"

    @tool
    @_catch_errors
    def get_active_alerts() -> str:
        """
        Get all currently active (pending or firing) alerts. Use this to
        answer "show me the latest alerts" or "what's currently firing?".
        """
        return _serialize(alert_agent.get_active_alerts())

    @tool
    @_catch_errors
    def get_critical_alerts() -> str:
        """
        Get only active alerts with severity=critical. Use this to answer
        "show the latest critical alerts".
        """
        return _serialize(alert_agent.get_critical_alerts())

    @tool
    @_catch_errors
    def get_incident_summary() -> str:
        """
        Get a rollup of current incidents: total active alerts, how many
        are critical, how many require immediate action, and the suggested
        notification route per alert.
        """
        return _serialize(alert_agent.get_incident_summary())

    @tool
    @_catch_errors
    def get_correlated_incidents() -> str:
        """
        Get active alerts grouped into correlated incidents by affected
        service and time proximity, instead of a flat alert list. Each
        incident reports the service, how many alerts fired together,
        whether it's a multi-alert incident (a stronger signal of a real
        outage vs. an isolated blip), and the max severity involved. Use
        this to answer "what's actually going on right now?" or "are any of
        these alerts related?" before diving into a single alert.
        """
        return _serialize(alert_correlator.get_correlated_incidents())

    @tool
    @_catch_errors
    def get_incident_history(service_name: str = "", since_hours: int = 168, limit: int = 20) -> str:
        """
        Get past auto-triaged incidents (Milestone 6's Alertmanager-webhook
        auto-triage — see auto_responder.py), newest first: alert name,
        service, severity, status, when it fired, and the RCA report/outcome
        the copilot produced for it at the time.

        Unlike every other tool here, this looks at the past rather than
        live cluster/metrics/trace state — use it to answer questions like
        "has checkout had issues before", "how many times has frontend
        alerted this week", or "what was the root cause last time
        product-catalog went down", none of which the current-state tools
        (get_service_metrics, investigate_service, etc.) can see.

        Args:
            service_name: Filter to incidents for this service (substring
                match, case-insensitive). Empty string (default) returns
                incidents for all services.
            since_hours: Only include incidents from this many hours ago
                to now. Defaults to 168 (7 days).
            limit: Maximum number of incidents to return, newest first.
                Defaults to 20.
        """
        records = incident_store.query_history(service=service_name, since_hours=since_hours, limit=limit)
        return _serialize([r.to_dict() for r in records])

    @tool
    @_catch_errors
    def search_similar_incidents(query: str, top_k: int = 5) -> str:
        """
        Fuzzy search over past incidents' RCA reports/outcomes for ones with
        wording similar to `query` — e.g. "checkout throwing 500s after a
        deploy" or "pod stuck in CrashLoopBackOff" — without needing to know
        the exact service or alert name. Ranked by keyword/phrase overlap
        (TF-IDF), not learned semantic similarity, so it's strongest when
        `query` shares actual wording with a past RCA report (symptom terms,
        service names, error types), not a pure paraphrase with no shared
        vocabulary.

        Use this instead of get_incident_history when the user describes a
        symptom or situation rather than naming an exact service or asking
        about a specific time window — e.g. "has something like this
        happened before?" or "have we seen this error before?". Prefer
        get_incident_history when they do name a specific service/timeframe.

        Args:
            query: Free-text description of the symptom/situation to search for.
            top_k: Maximum number of matching past incidents to return, ranked
                highest-similarity first. Results below a small similarity
                floor are dropped rather than padded in — an empty result
                means nothing past looks related, not that the search failed.
        """
        matches = _search_similar_incidents(incident_store.list(), query, top_k=top_k)
        return _serialize(
            [{"similarity": round(match.similarity, 3), **match.record.to_dict()} for match in matches]
        )

    @tool
    @_catch_errors
    def detect_service_anomalies(service_name: str, lookback_minutes: int = 60) -> str:
        """
        Run statistical anomaly detection (z-score vs. the service's own
        recent history) across CPU, memory, latency, and request rate for a
        service. Unlike the static alert rules, this can catch a metric
        behaving unusually for *that* service even if it hasn't crossed a
        fixed threshold yet — including a request-rate drop-to-near-zero
        (e.g. a routing failure) that a plain error-rate alert would miss.
        Use this for "is anything unusual with <service> right now?" or as
        an early-warning check before an alert has fired.
        """
        return _serialize(anomaly_detector.scan_service(service_name, lookback_minutes))

    @tool
    def remediate_scale_deployment(service_name: str, desired_replicas: int, reason: str) -> str:
        """
        Scale a deployment to a desired replica count to remediate an
        incident (e.g. a deployment that was scaled to 0 or otherwise has
        fewer replicas than it needs). This is the platform's only
        cluster-mutating action — everything else is read-only.

        IMPORTANT: This pauses the conversation for human approval before
        touching the cluster. Only call this after you've investigated and
        can state a clear, evidence-based reason (e.g. via investigate_service
        or get_deployment_health) — never call it speculatively. The human
        will see exactly the service_name, desired_replicas, and reason you
        pass in, so make `reason` a clear, specific justification.

        Args:
            service_name: The deployment to scale (e.g. "checkout").
            desired_replicas: The replica count to scale to.
            reason: Why this remediation is being proposed, grounded in
                evidence gathered from other tools.
        """
        approved = interrupt(
            {
                "action": "scale_deployment",
                "service_name": service_name,
                "desired_replicas": desired_replicas,
                "reason": reason,
            }
        )
        if not approved:
            return f"Remediation cancelled: scaling {service_name} to {desired_replicas} replicas was NOT approved."

        result = kubernetes_agent.scale_deployment(namespace, service_name, desired_replicas)
        return (
            f"Remediation applied: scaled {result['name']} to {result['replicas']} replicas "
            f"in namespace {result['namespace']}."
        )

    @tool
    def investigate_service(
        service_name: str, alert_name: str = "", severity: str = "critical"
    ) -> str:
        """
        Run the full Milestone 4 investigation pipeline for a service: pull
        metrics, traces, and Kubernetes evidence in parallel, correlate it,
        and have an LLM produce a structured root-cause analysis with
        supporting evidence and recommended next steps, rendered as a
        markdown report.

        This is the heaviest tool and makes its own LLM call — prefer the
        narrower get_service_metrics / get_slow_traces / get_pod_health
        tools for quick lookups, and reach for this one when the user wants
        a full "why is X broken/slow/down" root-cause investigation, or asks
        for recommended next troubleshooting steps for a specific incident.

        Args:
            service_name: The affected service (e.g. "checkout", "frontend-proxy").
            alert_name: Optional alert name to attribute the investigation to
                (defaults to a synthetic "ManualInvestigation" alert if omitted).
            severity: Severity to attach to the synthetic alert if no real
                alert is being investigated. Defaults to "critical".
        """
        alert = {
            "name": alert_name or "ManualInvestigation",
            "severity": severity,
            "status": "firing",
            "labels": {"service_name": service_name},
            "annotations": {},
        }
        result = investigation_graph.investigate(alert)
        return result.get("rca_report", "(no report generated)")

    @tool
    def generate_runbook(
        service_name: str, alert_name: str = "", severity: str = "critical"
    ) -> str:
        """
        Investigate a service and produce a step-by-step remediation runbook
        an on-call engineer could follow directly (concrete kubectl/PromQL
        commands where the evidence supports one, plus verification and
        rollback steps), grounded in the same root-cause analysis
        investigate_service produces. The runbook is also saved to disk as a
        markdown file under ai-platform/runbooks/ so it's a real document,
        not just chat text.

        Use this instead of investigate_service when the user explicitly
        asks for a "runbook", "playbook", "step-by-step guide", or "how do I
        fix this" — investigate_service alone only gives a short recommended-
        actions list, not an execution-ready document.

        Args:
            service_name: The affected service (e.g. "checkout", "product-catalog").
            alert_name: Optional alert name to attribute the investigation to
                (defaults to a synthetic "ManualInvestigation" alert if omitted).
            severity: Severity to attach to the synthetic alert if no real
                alert is being investigated. Defaults to "critical".
        """
        alert = {
            "name": alert_name or "ManualInvestigation",
            "severity": severity,
            "status": "firing",
            "labels": {"service_name": service_name},
            "annotations": {},
        }
        result = investigation_graph.generate_runbook(alert)
        report = result.get("runbook_report")
        if not report:
            return "Could not generate a runbook — the investigation didn't produce a root cause to build one from."

        os.makedirs(RUNBOOKS_DIR, exist_ok=True)
        filename = runbook_filename(result.get("service_name"), alert.get("name"))
        path = os.path.join(RUNBOOKS_DIR, filename)
        with open(path, "w") as f:
            f.write(report)

        return f"{report}\n\n_Saved to `{os.path.abspath(path)}`._"

    return [
        get_service_metrics,
        get_top_cpu_consumers,
        get_latency_by_route,
        get_slow_traces,
        get_trace_critical_path,
        get_pod_health,
        get_deployment_health,
        get_recent_k8s_events,
        get_pod_logs,
        get_active_alerts,
        get_critical_alerts,
        get_incident_summary,
        get_correlated_incidents,
        get_incident_history,
        search_similar_incidents,
        detect_service_anomalies,
        investigate_service,
        generate_runbook,
        remediate_scale_deployment,
    ]
