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

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.messages.utils import count_tokens_approximately, trim_messages
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
search_similar_incidents (semantic/keyword search over past RCA reports for "have we seen this \
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
- The tool also enforces guardrails before a human ever sees the request: a replica-count cap, a \
check that the service is a real deployment in the managed namespace, a check that some \
evidence-gathering tool call for that service happened earlier in this conversation, and a \
per-service cooldown. If it comes back "blocked" rather than pausing for approval, that's a \
guardrail rejecting the request outright — explain the block reason to the user rather than \
retrying with the same values. In particular, if you're asked to "fix" something without having \
called any investigation tool yet, call one first (get_deployment_health or investigate_service) \
rather than going straight to remediate_scale_deployment, or it will simply be blocked.
- After a remediation attempt (approved, cancelled, or blocked), tell the user plainly what happened.
- Use get_remediation_audit_log to answer "what's been tried on this service before" or "has this \
been remediated already".
"""

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "groq": "llama-3.3-70b-versatile",
}

# Cheap-tier models for the copilot's own conversational ReAct loop — same
# provider as DEFAULT_MODELS/LLM_PROVIDER, just a lighter model. This is the
# highest-volume LLM call site in the platform (every tool round-trip, every
# turn), unlike the RCA/runbook structured calls (InvestigationGraph.llm),
# which stay on the model configured by LLM_PROVIDER/LLM_MODEL since that's
# where the actual diagnostic judgment happens. Tool-routing decisions are a
# task smaller models are generally good at, and an eval run
# (ai_platform/evals/run_tool_use_evals.py) against llama-3.1-8b-instant
# confirmed routing accuracy holds — including the one safety-relevant
# anti-pattern case (calling remediate_scale_deployment without evidence
# first), which the model did attempt but which the structural guardrail in
# copilot_tools.py caught regardless, exactly as that guardrail is designed
# to. Override with COPILOT_LLM_MODEL if you want a different model for
# this tier specifically; there's deliberately no separate
# COPILOT_LLM_PROVIDER — the cheap tier always uses the same provider as
# LLM_PROVIDER, just a lighter model, so it never needs a second set of
# credentials configured.
DEFAULT_CHEAP_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "groq": "llama-3.1-8b-instant",
}


def _resolve_llm_config(cheap: bool = False) -> tuple[str, str]:
    """Resolve (provider, model) for either the default tier (LLM_PROVIDER/
    LLM_MODEL) or the cheap tier (same provider, DEFAULT_CHEAP_MODELS unless
    COPILOT_LLM_MODEL overrides it). Shared by `_build_llm` (to actually
    construct the chat model) and `SRECopilot.__init__` (to size the
    trimmed-history budget against whichever model tier is actually in use —
    see `_max_history_tokens_for`)."""
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}'; expected one of {list(DEFAULT_MODELS)}")
    if cheap:
        model = os.getenv("COPILOT_LLM_MODEL", DEFAULT_CHEAP_MODELS[provider])
    else:
        model = os.getenv("LLM_MODEL", DEFAULT_MODELS[provider])
    return provider, model


def _build_llm(llm: Optional[Any] = None, cheap: bool = False) -> Any:
    if llm is not None:
        return llm
    provider, model = _resolve_llm_config(cheap=cheap)

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, temperature=0)
    else:
        # Claude Sonnet 5 / 4.7+ reject any non-default temperature (400
        # "temperature is deprecated for this model") — adaptive thinking
        # controls its own sampling, so we can't pin temperature=0 here the
        # way we do for Groq. Omit the param and take the model default.
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model)


# Token budget for what actually gets sent to the LLM per turn, separate
# from what's kept in the checkpointer. Without this, InMemorySaver keeps
# the *entire* conversation — every question, every tool call, every raw
# tool result (pod logs, metrics, a full investigate_service RCA report) —
# and each new turn resends all of it, so token cost per turn grows with
# conversation length instead of staying roughly flat. A long chat session
# or an auto-triaged incident with a few investigate_service calls can
# burn through a provider's per-minute/per-day token cap much faster than
# the underlying question actually requires.
#
# 6000 leaves headroom under Groq's free-tier llama-3.3-70b-versatile
# 12,000 TPM cap even after adding the system prompt (~800 tokens) and a
# fresh tool call/result, while still keeping several turns of real
# back-and-forth for follow-up questions ("what about its error rate?")
# to resolve against.
MAX_HISTORY_TOKENS = 6000


def _trim_history(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    `pre_model_hook` for `create_react_agent`: trims the message list sent
    to the LLM this turn to the most recent `MAX_HISTORY_TOKENS` worth of
    messages, dropping the oldest ones first.

    Returns `{"llm_input_messages": ...}` rather than `{"messages": ...}` —
    the former only affects what this model call sees, leaving the full
    history untouched in the checkpointer. That matters here: the web UI's
    tool-call trace and `chat.py`'s transcript both read the checkpointed
    history, so trimming what's actually persisted would silently erase
    older steps from the user-facing conversation view, not just from the
    model's context.

    `start_on`/`end_on="human"` keep the trimmed window starting and ending
    on a human turn (never mid-tool-call), since Anthropic/Groq's APIs both
    reject a message list that opens with a dangling tool result or ends
    with an unresolved tool call.
    """
    trimmed = trim_messages(
        state["messages"],
        strategy="last",
        token_counter=count_tokens_approximately,
        max_tokens=MAX_HISTORY_TOKENS,
        start_on="human",
        end_on=("human", "tool"),
    )
    return {"llm_input_messages": trimmed}


# Groq's documented free-tier tokens-per-minute (TPM) caps, per model. Used
# by `_max_history_tokens_for` to size the trimmed-history budget against
# whichever model tier is actually in use, rather than assuming every model
# shares llama-3.3-70b-versatile's 12,000 TPM headroom — llama-3.1-8b-instant
# (the default cheap tier, see DEFAULT_CHEAP_MODELS) has only 6,000 TPM, so
# reusing MAX_HISTORY_TOKENS (sized for the 70b model) unchanged could size a
# single request at the smaller model's *entire* per-minute budget before the
# system prompt, tool schemas, or this turn's own tool results are even added
# — an observed 413 ("Request too large"), not a theoretical risk. Not
# exhaustive; a Groq model not listed here falls back to MAX_HISTORY_TOKENS.
GROQ_TPM_LIMITS = {
    "llama-3.3-70b-versatile": 12000,
    "llama-3.1-8b-instant": 6000,
}

# Fraction of the model's TPM cap reserved as safety margin, on top of the
# *measured* fixed overhead (system prompt + bound tool schemas — see
# `_measure_fixed_overhead_tokens`), for this turn's own tool call/result
# content. Two rounds of tuning this as a flat constant (800, then still
# 800 after reducing tool count) both still 413'd in practice — Groq's real
# tokenizer runs measurably higher than `count_tokens_approximately`'s
# estimate (observed ~15-20% gap on real requests), and a flat margin
# doesn't scale with that gap the way a fraction of the cap does. 20% is
# deliberately generous: undercounting here causes a 413, overcounting only
# costs a smaller history window.
_SAFETY_MARGIN_FRACTION = 0.20

# Floor so a very small TPM cap doesn't compute a budget of zero (or
# negative) history — better to keep a sliver of context than none.
_MIN_HISTORY_TOKENS = 500

# Absolute floor for the safety margin itself, so a hypothetical very-low-TPM
# model doesn't get a near-zero margin just because 20% of its cap is small.
_MIN_SAFETY_MARGIN = 500


def _measure_fixed_overhead_tokens(system_prompt: str, tools: List[Any]) -> int:
    """
    Actual measured token cost of what's sent on *every* request regardless
    of conversation history: the system prompt plus every bound tool's
    schema (name + description + JSON args schema, the same shape LangChain
    actually serializes to the API). Measured rather than hardcoded so
    `_max_history_tokens_for` scales correctly whichever tool set ends up
    bound (see `CORE_COPILOT_TOOL_NAMES` — the reduced cheap-tier set has a
    smaller fixed cost than the full 20-tool set) instead of drifting from
    reality the way a one-time hardcoded estimate did.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    total = count_tokens_approximately([SystemMessage(content=system_prompt)])
    for t in tools:
        text = f"{t.name} {t.description} {json.dumps(t.args)}"
        total += count_tokens_approximately([HumanMessage(content=text)])
    return total


# Tools bound to the cheap tier's ReAct loop by default — a curated subset of
# the full 20, dropping the narrower/less-common ones (measured ~504 of
# ~2,572 tool-schema tokens, on top of the ~1,259-token system prompt) to
# reduce the fixed per-request overhead that's the real ceiling on a
# small-TPM model like llama-3.1-8b-instant (see
# `_measure_fixed_overhead_tokens`/`_max_history_tokens_for` — no amount of
# history trimming fixes a request that's already too large before any
# history is added).
#
# Deliberately does NOT drop `detect_service_anomalies` or
# `get_remediation_audit_log`: both are called out *by name* in
# COPILOT_SYSTEM_PROMPT's own routing guidance, so removing them from the
# bound set while leaving the prompt unchanged would let the model try to
# call a tool that no longer exists — a hard error, not a graceful
# degradation. Everything dropped here is only ever referred to generically
# ("etc.") or not mentioned at all, so removing it just means one less
# option, never a dangling reference.
#
# Set COPILOT_FULL_TOOLS=true to opt back into the complete 20-tool set
# (e.g. if you've since moved the cheap tier to a higher-TPM model or
# provider and don't need this tradeoff).
CORE_COPILOT_TOOL_NAMES = {
    "get_service_metrics",
    "get_slow_traces",
    "get_pod_health",
    "get_deployment_health",
    "get_active_alerts",
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


def _select_tools(tools: List[Any], reduce_tools: bool) -> List[Any]:
    """
    Returns `tools` filtered down to `CORE_COPILOT_TOOL_NAMES` when
    `reduce_tools` is true (and `COPILOT_FULL_TOOLS` isn't set), else
    `tools` unchanged.
    """
    if not reduce_tools or os.getenv("COPILOT_FULL_TOOLS", "").lower() in ("1", "true", "yes"):
        return tools
    return [t for t in tools if t.name in CORE_COPILOT_TOOL_NAMES]


def _max_history_tokens_for(provider: str, model: str, fixed_overhead_tokens: int) -> int:
    """
    Trimmed-history token budget for a given provider/model: the model's own
    TPM cap, minus the *measured* fixed overhead every request pays
    regardless of history (`fixed_overhead_tokens` — see
    `_measure_fixed_overhead_tokens`), minus a safety margin sized as a
    fraction of the cap (`_SAFETY_MARGIN_FRACTION`) to also cover the gap
    between that measurement and Groq's real tokenizer. Capped at the
    historical `MAX_HISTORY_TOKENS` so a higher-TPM model doesn't get a
    needlessly larger window than before. Falls back to `MAX_HISTORY_TOKENS`
    for Anthropic or any unlisted Groq model.
    """
    if provider == "groq":
        tpm = GROQ_TPM_LIMITS.get(model)
        if tpm is not None:
            margin = max(_MIN_SAFETY_MARGIN, int(tpm * _SAFETY_MARGIN_FRACTION))
            budget = tpm - fixed_overhead_tokens - margin
            return max(_MIN_HISTORY_TOKENS, min(MAX_HISTORY_TOKENS, budget))
    return MAX_HISTORY_TOKENS


def _build_trim_history_hook(max_history_tokens: int):
    """
    Same trimming behavior as `_trim_history`, but against an
    instance-specific `max_history_tokens` instead of the fixed module-level
    `MAX_HISTORY_TOKENS` — used by `SRECopilot` so the budget matches
    whichever model tier it actually resolved to (see
    `_max_history_tokens_for`). `_trim_history` itself is left unchanged
    (and still directly used/tested as the fixed-budget default) rather than
    parameterized in place, so existing direct callers/tests of it don't
    need to change.
    """

    def _hook(state: Dict[str, Any]) -> Dict[str, Any]:
        trimmed = trim_messages(
            state["messages"],
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=max_history_tokens,
            start_on="human",
            end_on=("human", "tool"),
        )
        return {"llm_input_messages": trimmed}

    return _hook


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
            llm: Optional pre-built chat model for the copilot's own
                conversational ReAct loop. If omitted, one is built lazily
                using the *cheap* tier (same provider as LLM_PROVIDER, but
                DEFAULT_CHEAP_MODELS/COPILOT_LLM_MODEL instead of
                LLM_MODEL) — tool-routing decisions are the highest-volume
                LLM call site in the platform and a task smaller models
                handle well; see DEFAULT_CHEAP_MODELS's comment. This is
                deliberately a *different* model than
                `InvestigationGraph.llm` (RCA/runbook structured reasoning
                stays on the full LLM_PROVIDER/LLM_MODEL tier) — inject a
                fake here for tests.
            investigation_graph: Optional pre-built Milestone 4 InvestigationGraph
                (reused for the investigate_service tool). If omitted, one is
                built from the given agents; it builds its own `llm` lazily
                (LLM_PROVIDER/LLM_MODEL — the stronger tier), independent of
                this copilot's own `llm` above.
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
        self._llm = _build_llm(llm, cheap=True)
        self.alert_agent = alert_agent
        # Deliberately not passing `llm=self._llm` here (unlike before this
        # tiering was added): this copilot's own model is the cheap tier, but
        # RCA/runbook reasoning should stay on the stronger LLM_PROVIDER/
        # LLM_MODEL tier, so InvestigationGraph builds its own independently
        # (lazily, on first use) rather than inheriting this one.
        self.investigation_graph = investigation_graph or InvestigationGraph(
            metrics_agent=metrics_agent,
            trace_agent=trace_agent,
            kubernetes_agent=kubernetes_agent,
            namespace=namespace,
        )
        all_tools = tools or build_copilot_tools(
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
        # Bind the reduced tool set (see CORE_COPILOT_TOOL_NAMES) unless a
        # caller already handed us a specific `tools` list — that's the
        # actual "I want full control over exactly what's callable" signal,
        # not whether `llm` was explicitly given: the eval harness
        # (ai_platform/evals/run_tool_use_evals.py) always passes an
        # explicit `llm` too, just to attach a token-tracking callback to
        # the same real cheap-tier model — gating on `llm is None` would
        # have silently skipped the reduction in exactly the run meant to
        # exercise it.
        reduce_tools = tools is None
        self.tools = _select_tools(all_tools, reduce_tools)
        self._checkpointer = InMemorySaver()
        # Size the trimmed-history budget against whichever model this
        # copilot's own loop is actually using and whichever tools actually
        # ended up bound (see _max_history_tokens_for's/
        # _measure_fixed_overhead_tokens's docstrings) rather than the fixed
        # MAX_HISTORY_TOKENS, which was sized for llama-3.3-70b-versatile
        # and is too large relative to some cheap-tier models' own TPM caps
        # (llama-3.1-8b-instant's 6,000 TPM in particular — this was an
        # observed 413 "Request too large", not a hypothetical). Only
        # meaningful when `llm` wasn't explicitly injected (e.g. a fake test
        # model) — in that case _resolve_llm_config falls through to the
        # non-Groq default and this budget is just MAX_HISTORY_TOKENS,
        # matching prior behavior.
        cheap_provider, cheap_model = _resolve_llm_config(cheap=True)
        fixed_overhead_tokens = _measure_fixed_overhead_tokens(COPILOT_SYSTEM_PROMPT, self.tools)
        max_history_tokens = _max_history_tokens_for(cheap_provider, cheap_model, fixed_overhead_tokens)
        self._agent: CompiledStateGraph = create_react_agent(
            model=self._llm,
            tools=self.tools,
            prompt=COPILOT_SYSTEM_PROMPT,
            checkpointer=self._checkpointer,
            pre_model_hook=_build_trim_history_hook(max_history_tokens),
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
