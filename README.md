# Multi-Agent AI SRE Platform

A production-style multi-agent platform that performs automated incident
investigation and root cause analysis over a live Kubernetes workload.

Alerts fire → the platform investigates them autonomously → it produces a
grounded RCA and an execution-ready remediation runbook → a human approves
any change that touches the cluster.

**Stack**

- OpenTelemetry Demo (Astronomy Shop) — the application under observation
- Kubernetes (Kind)
- Prometheus / Alertmanager
- Jaeger + OpenTelemetry Collector
- Python 3.14, LangGraph, FastAPI

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the layered system diagram and
[`milestones.md`](milestones.md) for detailed per-milestone status.

---

## Prerequisites

- Docker Desktop
- kubectl
- Kind
- Helm 3.14+
- Python 3.14 (pinned in `.python-version`) and [uv](https://docs.astral.sh/uv/)
- Git

```bash
docker --version
kubectl version --client
kind version
helm version
uv --version
```

---

## Project Structure

```text
multi-agent-sre/
│
├── ai_platform/
│   ├── tools/          # Milestone 2-3: observability clients + agents
│   │                   #   plus Milestone 6 analysis: alert_correlator,
│   │                   #   anomaly_detector, incident_search, incident_store
│   ├── coordinator/    # Milestone 4: LangGraph investigation graph, RCA, runbooks
│   ├── copilot/        # Milestone 5: conversational ReAct agent + CLI
│   ├── webui/          # Milestone 6: FastAPI server, auto-triage, browser UI
│   └── runbooks/       # generated remediation docs (gitignored)
│
├── observability/
│   └── helm/           # Helm values for the demo app (incl. Alertmanager)
│
├── docs/
├── ARCHITECTURE.md
├── milestones.md
└── README.md
```

`ai_platform` is a real installable package (`uv sync` installs it in editable
mode) — every module imports its siblings as `ai_platform.tools.x`,
`ai_platform.coordinator.y`, etc., rather than relying on manual `sys.path`
setup. Each of the four subpackages has a `tests/` subdirectory holding its
own mocked unit tests.

---

## Setup

### 1. Create the cluster

```bash
kind create cluster --name ai-sre
kubectl get nodes
```

### 2. Deploy the OpenTelemetry Demo

Full setup notes, including why the default chart profile overcommits a local
Kind node, are in [`docs/setup-openetemetry-demo.md`](docs/setup-openetemetry-demo.md).

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

helm install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo --create-namespace \
  --values observability/helm/otel-demo-minimal-values.yaml
```

Verify:

```bash
kubectl get pods -n otel-demo
```

Key services: `frontend-proxy`, `frontend`, `checkout`, `cart`, `product-catalog`.

`otel-demo-minimal-values.yaml` also enables the demo chart's own
**Alertmanager** subchart (in the `otel-demo` namespace) and wires Prometheus
to it, so it drives the Milestone 6 auto-triage webhook — no separate
monitoring stack install needed.

### 3. Install Python dependencies

```bash
uv sync
cp .env.example .env   # then fill in your API key
```

To use the Web UI, also set `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` in
`.env` (Google OIDC sign-in — see the comments in `.env.example` for how to
create one). Without them, `/login` and every `/api/*` route return an
error; the CLI copilot doesn't need this.

---

## Access the Services

Each of these needs its own terminal.

| Service | Command | URL |
| --- | --- | --- |
| Storefront | `kubectl port-forward -n otel-demo svc/frontend-proxy 8080:8080` | http://localhost:8080 |
| Prometheus | `kubectl port-forward -n otel-demo svc/prometheus 9090:9090` | http://localhost:9090 |
| Jaeger | `kubectl port-forward -n otel-demo svc/jaeger 16686:16686` | http://localhost:16686 |
| Alertmanager | `kubectl port-forward -n otel-demo svc/otel-demo-alertmanager 9093:9093` | http://localhost:9093 |

---

## Running the Platform

### Web UI (recommended)

```bash
uv run python -m ai_platform.webui.server
```

Open http://localhost:8000 (sign in with Google) — three tabs: **Operations
Hub** (default landing view — incident KPIs, resolution rate, time-to-
mitigate, volume/outcomes chart), **Timeline** (every auto-triaged incident,
chronological), and **Chat** (conversational copilot with a live tool-call
trace). An approval modal gates any cluster-mutating action from any tab.

### CLI copilot

```bash
uv run python -m ai_platform.copilot.chat
```

Ask things like:

- "Why is checkout slow?"
- "Which service has the highest CPU usage?"
- "Show me the latest critical alerts"
- "Give me a runbook to fix this"
- "Has this happened before?"

### One-off investigation

```bash
uv run python -m ai_platform.coordinator.run_investigation
```

### Offline demos (no cluster or API key needed)

```bash
uv run python -m ai_platform.copilot.sample_demo
uv run python -m ai_platform.copilot.sample_remediation_demo
```

---

## Tests

The full suite is mocked — no cluster, API key, or network required.

```bash
uv sync        # installs the dev group (pytest, httpx)
uv run pytest
```

`ai_platform` is a real installable package (see `[tool.uv] package = true`
in `pyproject.toml`) — `uv sync` installs it in editable mode, so
`ai_platform.tools.x`-style imports resolve without any `sys.path` or
`PYTHONPATH` setup. `pyproject.toml`'s `testpaths` is what keeps the
live-cluster smoke scripts below out of the offline run:

```bash
uv run pytest ai_platform/coordinator/tests   # one milestone at a time
```

Smoke tests that *do* need a live cluster and port-forwards are plain
scripts, not pytest targets — run them directly:

```bash
uv run python ai_platform/tools/test_clients.py         # clients (Milestone 2)
uv run python ai_platform/tools/test_agents_smoke.py    # agents  (Milestone 3)
uv run python ai_platform/tools/test_milestone6_smoke.py
```

---

## Triggering an Incident

Inject a fault via the demo's feature flag UI, or scale a deployment down:

```bash
kubectl scale deployment product-catalog --replicas=0 -n otel-demo
```

With Alertmanager wired to the webhook, the platform picks the alert up on its
own, investigates it, and it shows up in the Timeline and Operations Hub
tabs. Restore with:

```bash
kubectl scale deployment product-catalog --replicas=1 -n otel-demo
```

See [`ai_platform/copilot/REMEDIATION_RUNBOOK.md`](ai_platform/copilot/REMEDIATION_RUNBOOK.md)
for the full end-to-end fault-injection walkthrough.

---

## Teardown

```bash
# Stop port-forwards with Ctrl+C in each terminal, then:
helm uninstall otel-demo -n otel-demo
kind delete cluster --name ai-sre
```

---

## Milestone Progress

| # | Milestone | Status |
| --- | --- | --- |
| 1 | Observability Foundation | ✅ Complete |
| 2 | Observability Tool Layer | ✅ Complete |
| 3 | Individual AI Agents | ✅ Complete |
| 4 | LangGraph Multi-Agent Orchestration | ✅ Complete |
| 5 | AI SRE Copilot | ✅ Complete |
| 6 | Advanced Features | ✅ Complete |

Knowledge base integration and true embedding-based incident search are
implemented: `search_knowledge_base` searches human-authored playbooks
under [`knowledge_base/`](knowledge_base/), and both it and
`search_similar_incidents` use real semantic similarity (Voyage AI
embeddings) when `VOYAGE_API_KEY` is set, falling back to TF-IDF keyword
search otherwise — see `ai_platform/tools/semantic_search.py`. See
[`milestones.md`](milestones.md) for full detail.

Since Milestone 6, the web UI also gained Google OIDC sign-in (replacing
the shared-secret model), a dedicated Incident Timeline tab, an Operations
Hub KPI dashboard (now the default landing tab), and PII/secret redaction
on data reaching the LLM or SQLite — see `ARCHITECTURE.md` and
`milestones.md`'s post-Milestone-6 sections.
