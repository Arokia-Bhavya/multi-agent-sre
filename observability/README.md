# Observability Platform

Helm values for the observability stack the Multi-Agent AI SRE Platform reads
from. Deployment steps live in [`../docs/setup-openetemetry-demo.md`](../docs/setup-openetemetry-demo.md).

## Architecture

```
OpenTelemetry Demo (Astronomy Shop)
        │  OTLP
        ▼
OpenTelemetry Collector          (bundled in the demo chart)
        │
 ┌──────┴───────┐
 ▼              ▼
Prometheus    Jaeger             (bundled in the demo chart, ns: otel-demo)
 │
 ├──────► Grafana                (kube-prometheus-stack, ns: monitoring)
 │
 └──────► Alertmanager ──────► SRE Copilot webhook
                                (ns: monitoring)      (/api/webhook/alertmanager)
```

The AI platform queries Prometheus and Jaeger to perform automated incident
investigation and root cause analysis. Alertmanager pushes firing alerts to the
copilot's webhook so incidents are triaged without a human noticing them first.

## Files

| File | Purpose |
| --- | --- |
| `helm/otel-demo-minimal-values.yaml` | Demo app install. Disables `kafka`, `opensearch`, `postgresql`, and `llm` (which overcommit a local Kind node), and defines the application alert rules: `FrontendHighErrorRate`, `FrontendHighLatency`, `FrontendHighMemoryUsage`, `ProductCatalogPodDown`. |
| `helm/kube-prometheus-values.yaml` | `kube-prometheus-stack` install. Provides Grafana and Alertmanager, and routes every firing/resolved alert to the copilot's auto-triage webhook. |

The demo chart bundles its own Prometheus, Jaeger, and OpenTelemetry Collector
in the `otel-demo` namespace, so no separate install is needed for those.

## Install

```bash
helm install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo --create-namespace \
  --values helm/otel-demo-minimal-values.yaml

helm upgrade --install monitoring \
  prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --values helm/kube-prometheus-values.yaml
```

> The demo chart does not support in-place upgrades between versions. Alert
> rules are baked into the install values rather than applied as a separate
> overlay, so changing them means reinstalling the release.

Check the evaluated rules once Prometheus is ready:

```bash
kubectl port-forward -n otel-demo svc/prometheus 9090:9090
curl http://localhost:9090/api/v1/rules
```
