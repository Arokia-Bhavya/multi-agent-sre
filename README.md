# Multi-Agent AI SRE Platform

A production-style multi-agent platform that performs automated incident
investigation and root cause analysis over a live Kubernetes workload.

Alerts fire → the platform investigates them autonomously → it produces a
grounded RCA and an execution-ready remediation runbook → a human approves
any change that touches the cluster.

**Stack**

- OpenTelemetry Demo (Astronomy Shop) — the application under observation
- Kubernetes (Kind)
- Prometheus / Alertmanager / Grafana
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
├── ai-platform/
│   ├── tools/          # Milestone 2-3: observability clients + agents
│   │                   #   plus Milestone 6 analysis: alert_correlator,
│   │                   #   anomaly_detector, incident_search, incident_store
│   ├── coordinator/    # Milestone 4: LangGraph investigation graph, RCA, runbooks
│   ├── copilot/        # Milestone 5: conversational ReAct agent + CLI
│   ├── webui/          # Milestone 6: FastAPI server, auto-triage, browser UI
│   └── runbooks/       # generated remediation docs (gitignored)
│
├── observability/
│   └── helm/           # Helm values for the demo app and monitoring stack
│
├── docs/
├── ARCHITECTURE.md
├── milestones.md
└── README.md
```

Each of the four `ai-platform/` packages has a `tests/` subdirectory holding
its own mocked unit tests.

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

### 3. Deploy the monitoring stack

The demo chart bundles its own Prometheus and Jaeger. `kube-prometheus-stack`
is installed alongside it to provide **Alertmanager**, which drives the
Milestone 6 auto-triage webhook.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install monitoring \
  prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --values observability/helm/kube-prometheus-values.yaml
```

### 4. Install Python dependencies

```bash
uv sync
cp .env.example .env   # then fill in your API key
```

---

## Access the Services

Each of these needs its own terminal.

| Service | Command | URL |
| --- | --- | --- |
| Storefront | `kubectl port-forward -n otel-demo svc/frontend-proxy 8080:8080` | http://localhost:8080 |
| Prometheus | `kubectl port-forward -n otel-demo svc/prometheus 9090:9090` | http://localhost:9090 |
| Jaeger | `kubectl port-forward -n otel-demo svc/jaeger 16686:16686` | http://localhost:16686 |
| Grafana | `kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80` | http://localhost:3000 |
| Alertmanager | `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093` | http://localhost:9093 |

Grafana admin password:

```bash
kubectl get secret monitoring-grafana -n monitoring \
  -o jsonpath="{.data.admin-password}" | base64 --decode
```

---

## Running the Platform

### Web UI (recommended)

```bash
uv run python ai-platform/webui/server.py
```

Open http://localhost:8000 — chat interface with a live tool-call trace, an
Incidents panel showing auto-triaged alerts, and an approval modal that gates
any cluster-mutating action.

### CLI copilot

```bash
uv run python ai-platform/copilot/chat.py
```

Ask things like:

- "Why is checkout slow?"
- "Which service has the highest CPU usage?"
- "Show me the latest critical alerts"
- "Give me a runbook to fix this"
- "Has this happened before?"

### One-off investigation

```bash
uv run python ai-platform/coordinator/run_investigation.py
```

### Offline demos (no cluster or API key needed)

```bash
uv run python ai-platform/copilot/sample_demo.py
uv run python ai-platform/copilot/sample_remediation_demo.py
```

---

## Tests

The full suite is mocked — no cluster, API key, or network required.

```bash
uv sync        # installs the dev group (pytest, httpx)
uv run pytest
```

180 tests across four directories. `pyproject.toml` configures both
`pythonpath` (so the flat imports resolve without any `PYTHONPATH` juggling)
and `testpaths`, which is what keeps the live-cluster smoke scripts out of
the offline run:

```bash
uv run pytest ai-platform/coordinator/tests   # one milestone at a time
```

Smoke tests that *do* need a live cluster and port-forwards are plain
scripts, not pytest targets — run them directly:

```bash
uv run python ai-platform/tools/test_clients.py         # clients (Milestone 2)
uv run python ai-platform/tools/test_agents_smoke.py    # agents  (Milestone 3)
uv run python ai-platform/tools/test_milestone6_smoke.py
```

---

## Triggering an Incident

Inject a fault via the demo's feature flag UI, or scale a deployment down:

```bash
kubectl scale deployment product-catalog --replicas=0 -n otel-demo
```

With Alertmanager wired to the webhook, the platform picks the alert up on its
own, investigates it, and files it in the Incidents panel. Restore with:

```bash
kubectl scale deployment product-catalog --replicas=1 -n otel-demo
```

See [`ai-platform/copilot/REMEDIATION_RUNBOOK.md`](ai-platform/copilot/REMEDIATION_RUNBOOK.md)
for the full end-to-end fault-injection walkthrough.

---

## Teardown

```bash
# Stop port-forwards with Ctrl+C in each terminal, then:
helm uninstall monitoring -n monitoring
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
| 6 | Advanced Features | 🟡 Mostly complete |

Milestone 6 remaining: Slack/Teams integration, MCP server support, visual
incident timeline, knowledge base integration, and true embedding-based
incident search. See [`milestones.md`](milestones.md) for detail.
