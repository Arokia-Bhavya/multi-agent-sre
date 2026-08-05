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
import time
from typing import Annotated, Any, Dict, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from langgraph.types import interrupt

from ai_platform.tools.metrics_agent import MetricsAgent
from ai_platform.tools.trace_agent import TraceAgent
from ai_platform.tools.kubernetes_agent import KubernetesAgent
from ai_platform.tools.alert_agent import AlertAgent
from ai_platform.tools.alert_correlator import AlertCorrelator
from ai_platform.tools.anomaly_detector import AnomalyDetector

from ai_platform.coordinator.graph import InvestigationGraph
from ai_platform.coordinator.rca import RCAFinding
from ai_platform.coordinator.runbook import runbook_filename
from ai_platform.tools.incident_store import IncidentStore
from ai_platform.tools.incident_search import search_similar_incidents as _search_similar_incidents
from ai_platform.tools.knowledge_base import search_knowledge_base as _search_knowledge_base
from ai_platform.tools.pii_redaction import redact_structure


# Runbooks get saved here so a generated remediation doc is a real file the
# user can open/share, not just chat text that's gone once the session ends.
# Relative to this file (ai_platform/copilot/) rather than cwd, since the
# copilot can be launched from either the repo root or ai_platform/.
RUNBOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "runbooks")

# --- Remediation guardrails --------------------------------------------------
# remediate_scale_deployment is the platform's only cluster-mutating tool, so
# its bounds live here rather than being left entirely to the LLM's judgment
# plus a human glancing at an approval prompt.

# Upper bound on any single scale request. Prevents a hallucinated or
# adversarial-prompted replica count (e.g. 500) from ever reaching the human
# approval prompt, let alone the cluster.
MAX_REMEDIATION_REPLICAS = int(os.getenv("REMEDIATION_MAX_REPLICAS", "10"))

# Minimum time between two *applied* remediations against the same service,
# so an approval loop (human clicking approve repeatedly, or a flapping
# alert re-triggering auto-triage) can't hammer the same deployment.
REMEDIATION_COOLDOWN_SECONDS = float(os.getenv("REMEDIATION_COOLDOWN_SECONDS", "300"))

# Tool names that count as "evidence gathered" grounding a remediation
# request. The system prompt already tells the model never to call
# remediate_scale_deployment speculatively — this is the same rule enforced
# structurally rather than trusted from the model's own `reason` text, which
# is otherwise unverified free text a human approver has no way to check
# against what evidence (if any) actually exists.
EVIDENCE_TOOL_NAMES = {
    "investigate_service",
    "generate_runbook",
    "get_deployment_health",
    "get_pod_health",
    "get_pod_logs",
    "get_recent_k8s_events",
    "get_service_metrics",
    "get_slow_traces",
    "get_trace_critical_path",
    "detect_service_anomalies",
    "get_correlated_incidents",
}


def _evidence_gathered_for(state: Dict[str, Any], service_name: str) -> bool:
    """
    Best-effort check: has some evidence-gathering tool been called earlier
    in this conversation, for this service (or a namespace-wide check with
    no service filter, which still counts)?

    Not a semantic check of the `reason` text itself — that would need
    grounding the LLM's own claim against the evidence, which is a harder
    problem than this platform's scope. This only verifies *some*
    investigation happened first, which is what the system prompt already
    asks for; this makes it structurally enforced instead of trusted.
    """
    for msg in state.get("messages", []) or []:
        for call in getattr(msg, "tool_calls", None) or []:
            if call.get("name") not in EVIDENCE_TOOL_NAMES:
                continue
            call_service = (call.get("args") or {}).get("service_name")
            if not call_service:
                return True  # a namespace-wide check (e.g. get_pod_health()) counts
            call_service = str(call_service).lower()
            if service_name.lower() in call_service or call_service in service_name.lower():
                return True
    return False


def _service_matches(call_service: Any, service_name: str) -> bool:
    """Shared substring-match rule for whether a tool call's `service_name`
    argument refers to the same service as `service_name` (or, if the call
    had no service filter at all, counts as a namespace-wide check)."""
    if not call_service:
        return True
    call_service = str(call_service).lower()
    return service_name.lower() in call_service or call_service in service_name.lower()


def _evidence_gathered_since(state: Dict[str, Any], service_name: str, since_message_count: int) -> bool:
    """
    Has some evidence-gathering tool (`EVIDENCE_TOOL_NAMES`) been called for
    `service_name` in the messages added *after* index `since_message_count`
    of `state["messages"]`? Used to invalidate `_investigation_cache`: a
    cached `RCAFinding` is only safe to reuse for `generate_runbook` if
    nothing new has been learned about the service since it was produced —
    otherwise the runbook would be grounded in stale evidence.
    """
    for msg in (state.get("messages", []) or [])[since_message_count:]:
        for call in getattr(msg, "tool_calls", None) or []:
            if call.get("name") in EVIDENCE_TOOL_NAMES and _service_matches(
                (call.get("args") or {}).get("service_name"), service_name
            ):
                return True
    return False


# --- Investigation cache (for generate_runbook reuse) -----------------------
# generate_runbook always re-ran the *entire* investigation — a fresh
# metrics/trace/Kubernetes fetch plus a fresh RCA LLM call — even when
# investigate_service had just produced an RCAFinding for the exact same
# service seconds earlier in the same conversation. That's a full duplicate
# LLM call (plus duplicate agent fetches) on the common "investigate, then
# ask for a runbook" flow. This cache lets generate_runbook reuse that
# RCAFinding instead, as long as it's still within a short TTL (state can
# genuinely change — see generate_runbook's own docstring on why it
# defaults to always re-investigating) *and* no evidence-gathering tool has
# been called for that service since it was cached (see
# `_evidence_gathered_since`) — either signal means something may have
# changed and it's worth paying for a fresh investigation again.
#
# Keyed by (thread_id, service_name) so entries don't leak across
# conversations when one SRECopilot instance serves multiple threads (the
# web UI does). thread_id comes from the tool's injected `RunnableConfig`,
# not from LangGraph's per-thread `InjectedState`, since state only exposes
# the current conversation's messages, not its thread_id.
INVESTIGATION_CACHE_TTL_SECONDS = float(os.getenv("INVESTIGATION_CACHE_TTL_SECONDS", "120"))


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

    Also runs the result through `redact_structure` as a defense-in-depth
    PII/secret scrub — most free-text fields (pod logs, event messages) are
    already redacted at the client layer, but this catches anything that
    slips through (e.g. a past RCA report's `content` echoing raw evidence)
    before it's serialized for the model.
    """
    return json.dumps(redact_structure(result), default=str)


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
            ai_platform/tools/incident_store.py), used by
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

    # Per-service cooldown state for remediate_scale_deployment. Scoped to
    # this closure (one per copilot/process), not persisted — a process
    # restart clearing it is an acceptable tradeoff for avoiding an extra
    # storage round-trip on every tool call; the persisted audit log (below)
    # is what survives restarts.
    _last_applied_remediation: Dict[str, float] = {}

    # (thread_id, service_name) -> {"alert", "correlated_findings",
    # "rca_finding", "cached_at", "message_count"}. See
    # `INVESTIGATION_CACHE_TTL_SECONDS`'s comment above for why this exists.
    # Same scoping tradeoff as `_last_applied_remediation`: in-process only,
    # cleared on restart.
    _investigation_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _thread_id(config: RunnableConfig) -> str:
        return (config.get("configurable") or {}).get("thread_id", "default")

    def _cache_investigation(
        thread_id: str,
        service_name: str,
        alert: Dict[str, Any],
        correlated_findings: Dict[str, Any],
        rca_finding: RCAFinding,
        state: Dict[str, Any],
    ) -> None:
        _investigation_cache[(thread_id, service_name)] = {
            "alert": alert,
            "correlated_findings": correlated_findings,
            "rca_finding": rca_finding,
            "cached_at": time.time(),
            # Anchor for _evidence_gathered_since: only messages added after
            # this point can invalidate the entry.
            "message_count": len(state.get("messages", []) or []),
        }

    def _fresh_cached_investigation(
        thread_id: str, service_name: str, state: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        entry = _investigation_cache.get((thread_id, service_name))
        if entry is None:
            return None
        if time.time() - entry["cached_at"] > INVESTIGATION_CACHE_TTL_SECONDS:
            return None
        if _evidence_gathered_since(state, service_name, entry["message_count"]):
            return None
        return entry

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
        Get active alerts grouped into correlated incidents by service and
        time proximity, instead of a flat list. Reports how many alerts
        fired together (a multi-alert incident is a stronger signal of a
        real outage vs. an isolated blip) and max severity. Use for "what's
        actually going on right now?" or "are any of these alerts related?"
        before diving into a single alert.
        """
        return _serialize(alert_correlator.get_correlated_incidents())

    @tool
    @_catch_errors
    def get_incident_history(service_name: str = "", since_hours: int = 168, limit: int = 20) -> str:
        """
        Get past auto-triaged incidents (alert, service, severity, status,
        when it fired, and the RCA/outcome), newest first. Looks at the
        past, not live state — use for "has X had issues before", "how many
        times has X alerted this week", "what was the root cause last time
        X went down".

        Args:
            service_name: Filter by service (substring, case-insensitive).
                Empty = all services.
            since_hours: Only incidents from this many hours ago to now
                (default 168 = 7 days).
            limit: Max incidents returned, newest first (default 20).
        """
        records = incident_store.query_history(service=service_name, since_hours=since_hours, limit=limit)
        return _serialize([r.to_dict() for r in records])

    @tool
    @_catch_errors
    def search_similar_incidents(query: str, top_k: int = 5) -> str:
        """
        Fuzzy keyword search (TF-IDF, not semantic) over past RCA reports
        for ones worded similarly to `query` — e.g. "pod stuck in
        CrashLoopBackOff" — without needing an exact service/alert name.
        Strongest when `query` shares actual wording with a past report.

        Use instead of get_incident_history when the user describes a
        symptom rather than naming a service/timeframe (e.g. "have we seen
        this before?"); use get_incident_history when they do name one.

        Args:
            query: Free-text symptom/situation description.
            top_k: Max matches to return, ranked by similarity. An empty
                result means nothing past looks related.
        """
        matches = _search_similar_incidents(incident_store.list(), query, top_k=top_k)
        return _serialize(
            [{"similarity": round(match.similarity, 3), **match.record.to_dict()} for match in matches]
        )

    @tool
    @_catch_errors
    def search_knowledge_base(query: str, top_k: int = 5) -> str:
        """
        Search human-authored playbooks/runbooks (knowledge_base/ at the
        repo root) for established guidance on a known failure pattern —
        e.g. "elevated 5xx error rate", "pod stuck in CrashLoopBackOff",
        "container memory climbing toward OOM". Semantic search when an
        embeddings provider is configured (VOYAGE_API_KEY), keyword
        fallback otherwise.

        Distinct from search_similar_incidents: that searches this
        platform's own auto-generated history of what happened last time a
        specific alert fired; this searches reference material written
        once by a human that applies whether or not this exact incident
        has ever fired before. Use this for "what's the standard procedure
        for X" or "is there a documented playbook for this" questions, or
        to ground a recommendation in established guidance rather than
        only this platform's own limited incident history.

        Args:
            query: Free-text description of the failure pattern or symptom.
            top_k: Max matches to return, ranked by similarity. An empty
                result means no playbook covers this.
        """
        matches = _search_knowledge_base(query, top_k=top_k)
        return _serialize(
            [
                {
                    "title": match.doc.title,
                    "similarity": round(match.similarity, 3),
                    "content": match.doc.content,
                }
                for match in matches
            ]
        )

    @tool
    @_catch_errors
    def detect_service_anomalies(service_name: str, lookback_minutes: int = 60) -> str:
        """
        Run statistical anomaly detection (z-score vs. the service's own
        history) across CPU, memory, latency, and request rate. Catches
        unusual behavior even before a static alert threshold is crossed
        (e.g. a request-rate drop-to-near-zero a plain error-rate alert
        would miss). Use for "is anything unusual with <service>?" or as an
        early-warning check before an alert fires.
        """
        return _serialize(anomaly_detector.scan_service(service_name, lookback_minutes))

    @tool
    def remediate_scale_deployment(
        service_name: str,
        desired_replicas: int,
        reason: str,
        state: Annotated[Dict[str, Any], InjectedState],
    ) -> str:
        """
        Scale a deployment to a desired replica count to remediate an
        incident (e.g. scaled to 0, or fewer replicas than needed). The
        platform's only cluster-mutating action — everything else is
        read-only.

        Pauses for human approval before touching the cluster. Only call
        after investigating and stating a clear, evidence-based reason (via
        investigate_service or get_deployment_health) — never
        speculatively. `reason` is shown to the human approving it, so make
        it specific.

        Guardrails enforced before a human sees it (a failing request is
        rejected immediately, not just discouraged): replica count must be
        in range; service must be a real deployment in the managed
        namespace; some evidence-gathering tool must have been called for
        this service earlier in the conversation (calling this as your
        first and only tool call will be blocked regardless of how
        convincing `reason` sounds); and the service can't be re-remediated
        within the cooldown window. Call get_deployment_health first if
        unsure a service name is correct.

        Args:
            service_name: The deployment to scale (e.g. "checkout").
            desired_replicas: The replica count to scale to.
            reason: Why this remediation is being proposed, grounded in
                evidence gathered from other tools.
        """
        if desired_replicas < 0 or desired_replicas > MAX_REMEDIATION_REPLICAS:
            detail = (
                f"desired_replicas={desired_replicas} is outside the allowed range "
                f"[0, {MAX_REMEDIATION_REPLICAS}]"
            )
            incident_store.log_remediation(
                service_name=service_name, desired_replicas=desired_replicas,
                reason=reason, status="blocked", detail=detail,
            )
            return f"Remediation blocked: {detail}. Not sent for approval."

        known_deployments = {
            d.get("name") for d in kubernetes_agent.get_deployment_health(namespace)
        }
        if service_name not in known_deployments:
            detail = f"'{service_name}' is not a known deployment in namespace '{namespace}'"
            incident_store.log_remediation(
                service_name=service_name, desired_replicas=desired_replicas,
                reason=reason, status="blocked", detail=detail,
            )
            return (
                f"Remediation blocked: {detail}. Not sent for approval. "
                f"Known deployments: {sorted(known_deployments)}"
            )

        if not _evidence_gathered_for(state, service_name):
            detail = (
                "no evidence-gathering tool (e.g. investigate_service, get_deployment_health, "
                f"get_pod_health) was called for '{service_name}' earlier in this conversation"
            )
            incident_store.log_remediation(
                service_name=service_name, desired_replicas=desired_replicas,
                reason=reason, status="blocked", detail=detail,
            )
            return f"Remediation blocked: {detail}. Not sent for approval."

        last_applied = _last_applied_remediation.get(service_name)
        if last_applied is not None:
            elapsed = time.time() - last_applied
            if elapsed < REMEDIATION_COOLDOWN_SECONDS:
                detail = (
                    f"{service_name} was remediated {elapsed:.0f}s ago; "
                    f"cooldown is {REMEDIATION_COOLDOWN_SECONDS:.0f}s"
                )
                incident_store.log_remediation(
                    service_name=service_name, desired_replicas=desired_replicas,
                    reason=reason, status="blocked", detail=detail,
                )
                return f"Remediation blocked: {detail}. Not sent for approval."

        approved = interrupt(
            {
                "action": "scale_deployment",
                "service_name": service_name,
                "desired_replicas": desired_replicas,
                "reason": reason,
            }
        )
        if not approved:
            incident_store.log_remediation(
                service_name=service_name, desired_replicas=desired_replicas,
                reason=reason, status="rejected",
            )
            return f"Remediation cancelled: scaling {service_name} to {desired_replicas} replicas was NOT approved."

        result = kubernetes_agent.scale_deployment(namespace, service_name, desired_replicas)
        _last_applied_remediation[service_name] = time.time()
        incident_store.log_remediation(
            service_name=service_name, desired_replicas=desired_replicas,
            reason=reason, status="approved",
        )
        return (
            f"Remediation applied: scaled {result['name']} to {result['replicas']} replicas "
            f"in namespace {result['namespace']}."
        )

    @tool
    @_catch_errors
    def get_remediation_audit_log(service_name: str = "", limit: int = 20) -> str:
        """
        Get the audit trail of past remediate_scale_deployment attempts,
        newest first: service, requested replicas, reason, and outcome
        ("blocked" — guardrail rejected before a human saw it, "rejected"
        — human declined, "approved" — applied), plus `detail` for blocked
        entries.

        Use for "what's been tried on <service>" or "has anyone tried to
        fix this before". `service_name` filters by substring; omit for all
        services.
        """
        return _serialize(incident_store.query_remediation_audit(service_name=service_name, limit=limit))

    @tool
    def investigate_service(
        service_name: str,
        state: Annotated[Dict[str, Any], InjectedState],
        config: RunnableConfig,
        alert_name: str = "",
        severity: str = "critical",
    ) -> str:
        """
        Run the full root-cause investigation for a service: pull metrics,
        traces, and Kubernetes evidence in parallel, correlate it, and have
        an LLM produce a structured root-cause analysis with evidence and
        next steps, rendered as markdown.

        Makes its own LLM call — prefer narrower tools (get_service_metrics,
        get_slow_traces, get_pod_health) for quick lookups. Use this for a
        full "why is X broken/slow/down" question or recommended next steps
        on a specific incident.

        Args:
            service_name: The affected service (e.g. "checkout").
            alert_name: Optional alert name to attribute this to (default:
                synthetic "ManualInvestigation").
            severity: Severity for the synthetic alert if none is real
                (default "critical").
        """
        alert = {
            "name": alert_name or "ManualInvestigation",
            "severity": severity,
            "status": "firing",
            "labels": {"service_name": service_name},
            "annotations": {},
        }
        result = investigation_graph.investigate(alert)
        if result.get("rca_finding") is not None:
            # Lets a follow-up generate_runbook call in this conversation
            # skip re-investigating from scratch — see _investigation_cache.
            _cache_investigation(
                _thread_id(config), service_name, alert,
                result["correlated_findings"], result["rca_finding"], state,
            )
        return result.get("rca_report", "(no report generated)")

    @tool
    def generate_runbook(
        service_name: str,
        state: Annotated[Dict[str, Any], InjectedState],
        config: RunnableConfig,
        alert_name: str = "",
        severity: str = "critical",
    ) -> str:
        """
        Investigate a service and produce a step-by-step remediation runbook
        (concrete kubectl/PromQL commands, verification, rollback), grounded
        in the same root-cause analysis investigate_service uses. Also saved
        to disk under ai_platform/runbooks/.

        Use instead of investigate_service when the user explicitly asks for
        a "runbook", "playbook", "step-by-step guide", or "how do I fix
        this" — investigate_service alone only gives a short action list.

        Args:
            service_name: The affected service (e.g. "checkout").
            alert_name: Optional alert name to attribute this to (default:
                synthetic "ManualInvestigation").
            severity: Severity for the synthetic alert if none is real
                (default "critical").
        """
        alert = {
            "name": alert_name or "ManualInvestigation",
            "severity": severity,
            "status": "firing",
            "labels": {"service_name": service_name},
            "annotations": {},
        }
        thread_id = _thread_id(config)
        cached = _fresh_cached_investigation(thread_id, service_name, state)
        if cached is not None:
            # Reuse the recent RCAFinding instead of re-investigating from
            # scratch — see _investigation_cache's comment for why this is
            # safe (short TTL, invalidated by any new evidence-gathering
            # call for this service since).
            result = investigation_graph.generate_runbook_from_finding(
                cached["alert"], service_name, cached["correlated_findings"], cached["rca_finding"],
            )
        else:
            result = investigation_graph.generate_runbook(alert)
            if result.get("rca_finding") is not None:
                _cache_investigation(
                    thread_id, service_name, alert,
                    result["correlated_findings"], result["rca_finding"], state,
                )

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
        search_knowledge_base,
        detect_service_anomalies,
        investigate_service,
        generate_runbook,
        remediate_scale_deployment,
        get_remediation_audit_log,
    ]
