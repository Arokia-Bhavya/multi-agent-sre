# Setup

Full setup, run, and teardown instructions for the Multi-Agent AI SRE
Platform. See [`../README.md`](../README.md) for the project overview.

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

## 1. Create the cluster

```bash
kind create cluster --name ai-sre
kubectl get nodes
```

## 2. Deploy the OpenTelemetry Demo

Full setup notes, including why the default chart profile overcommits a local
Kind node, are in
[`setup-openetemetry-demo.md`](setup-openetemetry-demo.md).

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
Prometheus, Jaeger, Grafana, and Alertmanager all come bundled in this one
install — see `observability/README.md` for how they're wired together.

## 3. Install Python dependencies

```bash
uv sync
cp .env.example .env   # then fill in your API key
```

## Access the services

Each of these needs its own terminal.

| Service | Command | URL |
| --- | --- | --- |
| Storefront | `kubectl port-forward -n otel-demo svc/frontend-proxy 8080:8080` | http://localhost:8080 |
| Prometheus | `kubectl port-forward -n otel-demo svc/prometheus 9090:9090` | http://localhost:9090 |
| Jaeger | `kubectl port-forward -n otel-demo svc/jaeger 16686:16686` | http://localhost:16686 |
| Grafana | `kubectl port-forward -n otel-demo svc/grafana 3000:80` | http://localhost:3000 |
| Alertmanager | `kubectl port-forward -n otel-demo svc/otel-demo-alertmanager 9093:9093` | http://localhost:9093 |

## Running the platform

**Web UI (recommended)**

```bash
uv run python ai_platform/webui/server.py
```

Open http://localhost:8000 — chat interface with a live tool-call trace, an
Incidents panel showing auto-triaged alerts, and an approval modal that gates
any cluster-mutating action.

**CLI copilot**

```bash
uv run python ai_platform/copilot/chat.py
```

Ask things like:

- "Why is checkout slow?"
- "Which service has the highest CPU usage?"
- "Show me the latest critical alerts"
- "Give me a runbook to fix this"
- "Has this happened before?"

**One-off investigation**

```bash
uv run python ai_platform/coordinator/run_investigation.py
```

**Offline demos (no cluster or API key needed)**

```bash
uv run python ai_platform/copilot/sample_demo.py
uv run python ai_platform/copilot/sample_remediation_demo.py
```

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
uv run pytest ai_platform/coordinator/tests   # one milestone at a time
```

Smoke tests that *do* need a live cluster and port-forwards are plain
scripts, not pytest targets — run them directly:

```bash
uv run python ai_platform/tools/test_clients.py
uv run python ai_platform/tools/test_agents_smoke.py
uv run python ai_platform/tools/test_milestone6_smoke.py
```

## Triggering an incident

Inject a fault via the demo's feature flag UI, or scale a deployment down:

```bash
kubectl scale deployment product-catalog --replicas=0 -n otel-demo
```

With Alertmanager wired to the webhook, the platform picks the alert up on
its own, investigates it, and files it in the Incidents panel. Restore with:

```bash
kubectl scale deployment product-catalog --replicas=1 -n otel-demo
```

See [`remediation-runbook.md`](remediation-runbook.md) for the full
end-to-end fault-injection walkthrough.

## Teardown

```bash
# Stop port-forwards with Ctrl+C in each terminal, then:
helm uninstall otel-demo -n otel-demo
kind delete cluster --name ai-sre
```
