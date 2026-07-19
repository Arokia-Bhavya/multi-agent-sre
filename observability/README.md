# Observability Platform

This directory contains the complete observability stack for the Multi-Agent AI SRE Platform.

## Components

- Prometheus
- Grafana
- Alertmanager
- Jaeger
- OpenTelemetry Collector

## Installation Order

1. kube-prometheus-stack
2. Jaeger
3. OpenTelemetry Collector

## Architecture

```
Google Online Boutique
        │
        ▼
OpenTelemetry Collector
        │
 ┌──────┼─────────────┐
 ▼      ▼             ▼
Prometheus       Jaeger      (Loki - Future)
        │
        ▼
Grafana
```

The AI platform queries Prometheus and Jaeger to perform automated incident investigation and root cause analysis.