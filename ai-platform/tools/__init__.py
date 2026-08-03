"""
Observability Tools for Multi-Agent SRE Platform

This package contains reusable Python clients for interacting with:
- Prometheus (metrics)
- Jaeger (distributed tracing)
- Kubernetes API (pod/deployment info)
- Alertmanager (incident classification; backed by Prometheus alerts)

...and the Milestone 3 agents built on top of them:
- MetricsAgent, TraceAgent, KubernetesAgent, AlertAgent
"""

from .prometheus_client import PrometheusClient
from .jaeger_client import JaegerClient
from .kubernetes_client import KubernetesClient
from .alertmanager import AlertmanagerClient, AlertSeverity
from .metrics_agent import MetricsAgent
from .trace_agent import TraceAgent
from .kubernetes_agent import KubernetesAgent
from .alert_agent import AlertAgent

__all__ = [
    "PrometheusClient",
    "JaegerClient",
    "KubernetesClient",
    "AlertmanagerClient",
    "AlertSeverity",
    "MetricsAgent",
    "TraceAgent",
    "KubernetesAgent",
    "AlertAgent",
]
