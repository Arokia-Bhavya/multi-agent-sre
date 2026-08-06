# LangGraph Coordinator

Milestone 4: a LangGraph workflow that orchestrates the Milestone 3 agents
(`MetricsAgent`, `TraceAgent`, `KubernetesAgent`) to investigate a firing
alert end-to-end and produce a root cause analysis (RCA) report.

## Workflow

```
receive_alert
    -> invoke_metrics_agent      \
    -> invoke_trace_agent         }-- run in parallel
    -> invoke_kubernetes_agent   /
    -> correlate_findings   (deterministic data-shaping, no LLM)
    -> produce_rca_report   (LLM reasoning, via langchain-anthropic)
```

`receive_alert` resolves the affected service name from the alert — an
explicit `service_name`/`service`/`job` label first, otherwise a token match
of the alert name against known OTel Demo services (see
`ai-platform/tools/service_matching.py`). Matching is done on word
boundaries: `FrontendProxyHighLatency` splits to
`[frontend, proxy, high, latency]` and matches `frontend-proxy`, while an
infrastructure alert like `KubeNodeNotReady` matches nothing and correctly
resolves to `None` rather than being attributed to an unrelated service.
The three agent nodes then run concurrently since none of them depend on
each other's output.
`correlate_findings` is plain Python — it trims and shapes the raw findings
into a compact evidence bundle. `produce_rca_report` is where the actual
reasoning happens: an LLM reads that evidence and returns a structured
`RCAFinding` (root cause, confidence, evidence, recommended actions), which
is rendered into a markdown report.

## Requirements

- An LLM API key for the `produce_rca_report` step. Not required for unit
  tests — inject a fake `llm` into `InvestigationGraph` instead.
  - Default: `ANTHROPIC_API_KEY` (uses Claude via `ChatAnthropic`).
  - Or set `LLM_PROVIDER=groq` and `GROQ_API_KEY` to use Groq instead — Groq
    has a free tier, useful for trying this out at no cost. Default model is
    `llama-3.3-70b-versatile`; override with `LLM_MODEL` (e.g.
    `llama-3.1-8b-instant` for a faster/lighter option).
- Prometheus and Jaeger port-forwarded, same as `ai-platform/tools/test_clients.py`.
- A working kubeconfig context for the target cluster.

## Usage

```python
from prometheus_client import PrometheusClient
from jaeger_client import JaegerClient
from kubernetes_client import KubernetesClient
from metrics_agent import MetricsAgent
from trace_agent import TraceAgent
from kubernetes_agent import KubernetesAgent
from graph import InvestigationGraph

graph = InvestigationGraph(
    metrics_agent=MetricsAgent(PrometheusClient("http://localhost:9090")),
    trace_agent=TraceAgent(JaegerClient("http://localhost:16686")),
    kubernetes_agent=KubernetesAgent(KubernetesClient()),
    namespace="otel-demo",
)

result = graph.investigate(alert)  # alert dict, e.g. from AlertmanagerClient.get_active_alerts()
print(result["rca_report"])
```

## Running

Unit tests (mocked agents + fake LLM, no cluster or API key needed). Run from
the repo root — `pyproject.toml` puts the source directories on `sys.path`,
so no `PYTHONPATH` juggling is needed:

```bash
uv run pytest ai-platform/coordinator/tests    # this milestone only
uv run pytest                                  # whole suite
```

Smoke test against a live cluster + real LLM call (Claude, default):

```bash
export ANTHROPIC_API_KEY=sk-...
uv run python ai-platform/coordinator/run_investigation.py
# or, if nothing is currently firing:
uv run python ai-platform/coordinator/run_investigation.py --alert-name FrontendHighErrorRate --service frontend
```

Or with Groq's free tier instead:

```bash
export LLM_PROVIDER=groq
export GROQ_API_KEY=gsk_...
uv run python ai-platform/coordinator/run_investigation.py --alert-name FrontendHighErrorRate --service frontend
```

## Notes

- Agent failures (e.g. Prometheus unreachable) are caught per-node and
  surfaced as `data_gaps` in the correlated findings rather than crashing
  the graph — the RCA report will note the gap and lower its confidence
  accordingly.
- `route_alert`-style notification delivery is out of scope here; this
  graph investigates and reports, it doesn't page anyone. That's Milestone 6.
