"""
AI SRE Copilot conversational agent (Milestone 5).

Wraps the copilot tool layer (`copilot_tools.py`) in a LangGraph ReAct agent so the
platform can be driven through natural-language questions, e.g.:

    "Why is checkout slow?"
    "Which service has the highest CPU usage?"
    "Why did the frontend-proxy become unavailable?"
    "Show me the latest critical alerts"
    "What should I do next about the checkout incident?"

The agent decides which tool(s) to call (metrics/traces/k8s/alerts, or the
full Milestone 4 investigation pipeline for root-cause questions), reads
the results, and replies conversationally. Conversation memory is kept
per-`thread_id` via LangGraph's checkpointer, so follow-up questions like
"what about its error rate?" resolve against prior turns.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command

from ai_platform.tools.alert_agent import AlertAgent
from ai_platform.tools.alert_correlator import AlertCorrelator
from ai_platform.tools.anomaly_detector import AnomalyDetector
from ai_platform.tools.kubernetes_agent import KubernetesAgent
from ai_platform.tools.metrics_agent import MetricsAgent
from ai_platform.tools.trace_agent import TraceAgent

from ai_platform.coordinator.graph import InvestigationGraph
from ai_platform.copilot.copilot_tools import build_copilot_tools


COPILOT_SYSTEM_PROMPT = """You are the AI SRE Copilot for the OpenTelemetry Demo \
(Astronomy Shop) platform, a conversational interface over a Multi-Agent SRE system.

You have tools to query live metrics (Prometheus), distributed traces (Jaeger), \
Kubernetes workload health, and active alerts, plus a full root-cause investigation \
tool that correlates all three and reasons over the evidence, plus one action tool, \
remediate_scale_deployment, that can actually change the cluster. You also have \
get_correlated_incidents (groups related active alerts into incidents), \
detect_service_anomalies (statistical anomaly detection on a service's own metric \
history, catching unusual behavior even before a static alert threshold is crossed), \
get_incident_history (past auto-triaged incidents — RCA reports and outcomes — for \
"what's happened with X recently"-style questions, distinct from current-state tools), and \
search_similar_incidents (fuzzy keyword search over past RCA reports for "have we seen this \
before" questions where the user describes symptoms rather than naming an exact service), and \
search_knowledge_base (search human-authored playbooks for known failure patterns, independent \
of whether this platform has ever actually seen that exact incident before).

Guidelines:
- Prefer the narrow, cheap tools (get_service_metrics, get_slow_traces, get_pod_health, \
get_active_alerts, etc.) for direct lookups.
- When asked "what's going on right now" or whether alerts are related, prefer \
get_correlated_incidents over get_active_alerts — it groups alerts by service and time, \
which better reflects whether there's one real incident or several unrelated blips.
- When asked whether something looks unusual/off for a service (especially before any \
alert has fired), use detect_service_anomalies rather than guessing from raw metric values.
- When asked about the past with a specific service or timeframe named ("how often does X \
alert", "what was the root cause last time X was down"), use get_incident_history rather than \
investigate_service or the other current-state tools, which only see live data.
- When asked about the past WITHOUT a specific service named — the user describes symptoms \
instead ("have we seen this error before?", "has something like this happened before?") — use \
search_similar_incidents instead of get_incident_history, which needs a service/time filter to \
be useful.
- When asked for standard procedure, established guidance, or "is there a documented playbook" \
for a failure pattern, use search_knowledge_base rather than search_similar_incidents — the \
knowledge base holds human-authored playbooks that apply whether or not this platform has \
actually seen the exact incident before, while search_similar_incidents only finds matches in \
this platform's own auto-generated history.
- Reach for investigate_service when the user asks a "why is X broken/slow/down" root-cause \
question, or explicitly wants recommended next steps for an incident — it runs the full \
Milestone 4 pipeline and already reasons over the evidence, so don't re-derive root cause \
yourself from raw tool output when you could just call it.
- investigate_service returns a finished, already-formatted markdown report. Return it to the \
user verbatim — do not re-summarize, re-explain, or re-derive conclusions from it. That's a \
wasted extra reasoning pass over evidence the tool already reasoned over.
- When the user explicitly asks for a "runbook", "playbook", "step-by-step guide", or "how do I \
fix this" (rather than just "why is X broken"), use generate_runbook instead of investigate_service \
— it produces an execution-ready document with concrete commands, verification, and rollback steps, \
and saves it to disk. Like investigate_service's report, return its output verbatim.
- Always ground your answer in tool output; never invent metrics, traces, pod names, or alerts.
- If a tool call fails or returns empty/no data, say so plainly rather than guessing.
- Be concise. Lead with the direct answer, then the supporting evidence, then (if relevant) \
recommended next steps.

Remediation:
- Only call remediate_scale_deployment after you have investigated and identified a concrete, \
evidence-backed root cause (e.g. via investigate_service or get_deployment_health showing fewer \
ready replicas than desired). Never call it speculatively or just because the user said "fix it" \
without evidence of what's actually wrong.
- The tool itself pauses for human approval before touching the cluster — you do not need to (and \
should not) ask "should I proceed?" in your own text first; just call the tool with a clear `reason`, \
and the approval gate happens automatically.
- After a remediation attempt (approved or cancelled), tell the user plainly what happened.
"""

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "groq": "llama-3.3-70b-versatile",
}


def _build_llm(llm: Optional[Any] = None) -> Any:
    if llm is not None:
        return llm
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}'; expected one of {list(DEFAULT_MODELS)}")
    model = os.getenv("LLM_MODEL", DEFAULT_MODELS[provider])

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, temperature=0)
    else:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model)


@dataclass(frozen=True)
class CopilotReply:
    """
    Result of a turn with the copilot.

    `pending_action` is `None` for an ordinary finished reply. When it is
    *not* `None`, the conversation is paused mid-tool-call awaiting human
    approval (currently only `remediate_scale_deployment` triggers this) —
    the dict is exactly the payload passed to `interrupt()` in the tool
    (`action`, `service_name`, `desired_replicas`, `reason`). Show it to the
    user, then call `SRECopilot.respond_to_approval(...)` with their decision
    to continue the turn. `content` may be empty in this case, since the
    model hadn't produced text yet when it paused.

    `steps` is a structured trace of the tool calls made during this turn, in
    order, e.g.
    [{"type": "call", "name": "get_pod_health", "args": {...}},
     {"type": "result", "name": "get_pod_health"}]. Populated alongside the
    stdout progress lines `_announce` prints, so a non-CLI caller (e.g. the
    web UI) can render the same "thinking" trace without scraping stdout.

    This is a frozen dataclass rather than a NamedTuple specifically because
    of `steps`: a NamedTuple's default value is created once at class-definition
    time and shared by every instance built without an explicit `steps=`, so
    anything that mutated one reply's trace would silently corrupt every other
    default-constructed reply — and every future one. `default_factory` builds
    a fresh list per instance instead. Nothing depends on tuple behaviour here
    (no unpacking, no index access), so the swap is transparent to callers.
    """

    content: str
    pending_action: Optional[Dict[str, Any]] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)


class SRECopilot:
    """
    Conversational front-end over the Milestone 3 agents and Milestone 4
    coordinator.

    Example:
        copilot = SRECopilot(
            metrics_agent=MetricsAgent(PrometheusClient("http://localhost:9090")),
            trace_agent=TraceAgent(JaegerClient("http://localhost:16686")),
            kubernetes_agent=KubernetesAgent(KubernetesClient()),
            alert_agent=AlertAgent(AlertmanagerClient("http://localhost:9090")),
            namespace="otel-demo",
        )
        reply = copilot.ask("Why is checkout slow?")
        reply = copilot.ask("What about its error rate?")  # same thread, remembers context
    """

    def __init__(
        self,
        metrics_agent: MetricsAgent,
        trace_agent: TraceAgent,
        kubernetes_agent: KubernetesAgent,
        alert_agent: AlertAgent,
        namespace: str = "otel-demo",
        llm: Optional[Any] = None,
        investigation_graph: Optional[InvestigationGraph] = None,
        tools: Optional[List[Any]] = None,
        alert_correlator: Optional[AlertCorrelator] = None,
        anomaly_detector: Optional[AnomalyDetector] = None,
        incident_store: Optional[Any] = None,
    ):
        """
        Args:
            metrics_agent, trace_agent, kubernetes_agent, alert_agent: Milestone 3 agents.
            namespace: Kubernetes namespace the k8s tools inspect.
            llm: Optional pre-built chat model. If omitted, one is built lazily
                based on LLM_PROVIDER ("anthropic" default, or "groq"), same as
                the Milestone 4 coordinator. Inject a fake here for tests.
            investigation_graph: Optional pre-built Milestone 4 InvestigationGraph
                (reused for the investigate_service tool). If omitted, one is
                built from the given agents, sharing `llm`.
            tools: Optional pre-built tool list, mainly for tests. If omitted,
                built via `build_copilot_tools`.
            alert_correlator: Optional pre-built Milestone 6 AlertCorrelator. If
                omitted, one is built from `alert_agent`.
            anomaly_detector: Optional pre-built Milestone 6 AnomalyDetector. If
                omitted, one is built from `metrics_agent`'s Prometheus client.
            incident_store: Optional `ai_platform.tools.incident_store.IncidentStore`
                backing the `get_incident_history` tool, so the copilot can answer
                "what's happened with X recently" questions. If omitted, an
                in-memory-only (":memory:") store is used — safe default for
                tests, but with no history to actually query; real callers
                (chat.py, server.py) pass a store backed by a real file so
                history survives restarts.
        """
        self._llm = _build_llm(llm)
        self.alert_agent = alert_agent
        self.investigation_graph = investigation_graph or InvestigationGraph(
            metrics_agent=metrics_agent,
            trace_agent=trace_agent,
            kubernetes_agent=kubernetes_agent,
            namespace=namespace,
            llm=self._llm,
        )
        self.tools = tools or build_copilot_tools(
            metrics_agent=metrics_agent,
            trace_agent=trace_agent,
            kubernetes_agent=kubernetes_agent,
            alert_agent=alert_agent,
            investigation_graph=self.investigation_graph,
            namespace=namespace,
            alert_correlator=alert_correlator,
            anomaly_detector=anomaly_detector,
            incident_store=incident_store,
        )
        self._checkpointer = InMemorySaver()
        self._agent: CompiledStateGraph = create_react_agent(
            model=self._llm,
            tools=self.tools,
            prompt=COPILOT_SYSTEM_PROMPT,
            checkpointer=self._checkpointer,
        )

    def ask(self, question: str, thread_id: str = "default", max_attempts: int = 3) -> CopilotReply:
        """
        Ask the copilot a question and get back its reply.

        `thread_id` scopes conversation memory: reuse the same id for a
        multi-turn conversation, use a new one to start fresh.

        If the model calls a remediation tool, the turn pauses for human
        approval and the returned `CopilotReply.pending_action` will be set —
        see `respond_to_approval`.

        Groq's Llama tool-calling occasionally emits a malformed function
        call mid-conversation (surfaces as `groq.BadRequestError`) instead of
        a proper tool_calls response — see the same issue called out in
        `coordinator/graph.py::_invoke_structured_rca`. Since the checkpointer
        persists progress after every completed step, a failed turn can be
        resumed in place: retry by invoking with `None` input (rather than
        re-sending the question), which continues from the last successful
        checkpoint instead of appending a duplicate user message.
        """
        config = {"configurable": {"thread_id": thread_id}}
        input_ = {"messages": [{"role": "user", "content": question}]}
        return self._run(input_, config, max_attempts)

    def respond_to_approval(
        self, approved: bool, thread_id: str = "default", max_attempts: int = 3
    ) -> CopilotReply:
        """
        Resume a conversation that's paused on `CopilotReply.pending_action`,
        with the human's approve/reject decision for the proposed action.

        `thread_id` must match the one used for the `ask()` call that
        produced the pending action.
        """
        config = {"configurable": {"thread_id": thread_id}}
        return self._run(Command(resume=approved), config, max_attempts)

    def _run(self, input_: Any, config: Dict[str, Any], max_attempts: int) -> CopilotReply:
        """
        Run a turn to completion, streaming intermediate steps to stdout as
        they happen (tool calls, tool results) rather than blocking silently
        until the whole turn finishes. Root-cause questions in particular can
        take several seconds (2-3 LLM round trips plus parallel Prometheus/
        Jaeger/K8s calls), so this is purely a perceived-latency fix — total
        wall-clock time is unchanged, but the CLI now shows what it's doing.
        """
        last_error: Optional[Exception] = None
        result: Optional[Dict[str, Any]] = None
        steps: List[Dict[str, Any]] = []

        for attempt in range(max_attempts):
            seen = 0
            steps = []
            try:
                for chunk in self._agent.stream(input_, config=config, stream_mode="values"):
                    result = chunk
                    messages = chunk.get("messages", [])
                    for msg in messages[seen:]:
                        steps.extend(self._announce(msg))
                    seen = len(messages)
                break
            except Exception as exc:  # e.g. groq.BadRequestError (malformed tool call)
                # Every attempt's exception gets surfaced here, not just the
                # last one: previously only the final attempt's error was
                # ever raised, which meant a genuine first-attempt failure
                # (e.g. a tool call with a bad/hallucinated argument) was
                # silently discarded in favor of whatever secondary error
                # came from retrying against an already-broken checkpoint —
                # usually a much less informative "invalid chat history"
                # error that obscured the real root cause.
                print(
                    f"  [attempt {attempt + 1}/{max_attempts}] {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                last_error = exc
                input_ = None  # subsequent retries resume from the checkpoint, not re-send input
        else:
            raise last_error

        pending = result.get("__interrupt__") if result else None
        if pending:
            return CopilotReply(content="", pending_action=pending[0].value, steps=steps)
        return CopilotReply(content=result["messages"][-1].content, steps=steps)

    @staticmethod
    def _announce(msg: Any) -> List[Dict[str, Any]]:
        """
        Print a one-line progress marker for a tool call or tool result (CLI
        usage), and return the same information as structured step entries
        (web UI usage) so both front ends can show the same "thinking" trace
        without one having to scrape the other's output.
        """
        recorded: List[Dict[str, Any]] = []
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for call in tool_calls:
                args = call.get("args", {})
                args_str = f"({args})" if args else "()"
                print(f"  … calling {call['name']}{args_str}", flush=True)
                recorded.append({"type": "call", "name": call["name"], "args": args})
        elif msg.__class__.__name__ == "ToolMessage":
            name = getattr(msg, "name", "tool")
            print(f"  ✓ {name} returned", flush=True)
            recorded.append({"type": "result", "name": name})
        return recorded
