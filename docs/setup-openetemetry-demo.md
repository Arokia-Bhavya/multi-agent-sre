# Setup: OpenTelemetry Demo on Kubernetes (Local)

Steps to deploy the OpenTelemetry Demo (Astronomy Shop) into a local Kind cluster.

## Prerequisites

- Docker Desktop
- kubectl
- Kind
- Helm 3.14+

## 1. Create the Kind cluster

```bash
kind create cluster --name ai-sre
kubectl get nodes
```

## 2. Add the OpenTelemetry Helm chart repo

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
```

## 3. Install the demo

The chart's default "full" profile (2.2.0) overcommits a local Kind node —
`kafka`, `opensearch`, `postgresql`, and `llm` (plus everything that depends
on them) aren't needed for this project and were the root cause of node
memory pressure (93% requests / 110% limits) and the `flagd-ui` sidecar
OOMKilling. `observability/helm/otel-demo-minimal-values.yaml` disables that
set. Apply it at install time (component enablement isn't something you want
to bolt on after the fact):

```bash
helm install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo --create-namespace \
  --values observability/helm/otel-demo-minimal-values.yaml
```

The same values file also defines this project's application alert rules —
`FrontendHighErrorRate`, `FrontendHighLatency`, `FrontendHighMemoryUsage`, and
`ProductCatalogPodDown` — so no separate overlay step is needed.

> Note: the chart does not support in-place upgrades between versions. To change versions, delete the release/resources and reinstall.

## 4. Verify pods

```bash
kubectl get pods -n otel-demo
```

## 5. Access the storefront

```bash
kubectl port-forward -n otel-demo svc/frontend-proxy 8080:8080
```

Open `http://localhost:8080`

## 6. Access Prometheus

```bash
kubectl port-forward -n otel-demo svc/prometheus 9090:9090
```

Open `http://localhost:9090`. Confirm the alert rules from step 3 were
evaluated:

```bash
curl http://localhost:9090/api/v1/rules
```

## 7. Access Jaeger

```bash
kubectl port-forward -n otel-demo svc/jaeger 16686:16686
```

Open `http://localhost:16686`

## 8. Access Alertmanager

`observability/helm/otel-demo-minimal-values.yaml` also enables the demo
chart's own Alertmanager subchart and wires Prometheus to it, so it's
already running in the `otel-demo` namespace — no separate install needed.
It drives the Milestone 6 auto-triage webhook.

```bash
kubectl port-forward -n otel-demo svc/otel-demo-alertmanager 9093:9093
```

## Notes

- The `opentelemetry-demo` source repo (e.g. cloned at `/Users/arokiabhavya/code/opentelemetry-demo`) no longer ships Kubernetes manifests — it's Docker Compose only. The Helm chart lives in the separate `open-telemetry/opentelemetry-helm-charts` repo. The local clone is still useful for building custom service images from source or referencing chart `values.yaml` structure.
- Key services: `frontend-proxy`, `frontend`, `checkout`, `cart`, `product-catalog`.
- Namespace: `otel-demo`.