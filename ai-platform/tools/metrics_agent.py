"""
Metrics Agent for Multi-Agent SRE Platform

Answers questions about request rate, error rate, latency, CPU, and memory
for services in the OpenTelemetry Demo, using the real metric names and
labels confirmed against the demo's Prometheus instance:

- HTTP metrics are labeled by `service_name`, `http_response_status_code`,
  `http_route` (e.g. http_server_request_duration_seconds_count/_bucket).
- Container resource metrics come from cAdvisor and are labeled only by
  `container` (NOT `container_name` — that was wrong in an earlier version
  of this file and silently returned no data for every call). The metric
  names are `container_cpu_usage_seconds_total` (a counter; utilization
  requires `rate()`, there is no pre-computed `container_cpu_utilization_ratio`
  gauge) and `container_memory_usage_bytes` (NOT `container_memory_usage_total_bytes`,
  which doesn't exist). Re-confirmed directly against this cluster via the
  Prometheus HTTP API on 2026-07-29. There is no service_name label on
  these, so callers pass container_name directly (it matches service_name
  for almost all OTel Demo services).
"""

from typing import Dict, List, Any, Optional
from prometheus_client import PrometheusClient


class MetricsAgent:
    """
    Agent responsible for answering metrics-related questions using
    Prometheus data from the OpenTelemetry Demo.

    Example:
        agent = MetricsAgent(PrometheusClient("http://localhost:9090"))
        agent.get_request_rate("shipping")
        agent.get_error_rate("shipping")
        agent.get_latency_p95("shipping")
        agent.get_cpu_usage("shipping")
        agent.get_memory_usage("shipping")
    """

    def __init__(self, prometheus_client: PrometheusClient):
        self.prom = prometheus_client

    def get_request_rate(self, service_name: str, window: str = "5m") -> float:
        """
        Requests per second for a service, summed across all routes/methods.
        """
        query = (
            f'sum(rate(http_server_request_duration_seconds_count'
            f'{{service_name="{service_name}"}}[{window}]))'
        )
        result = self.prom.query(query)
        return float(result[0]["value"][1]) if result else 0.0

    def get_error_rate(self, service_name: str, window: str = "5m") -> float:
        """
        Fraction (0.0-1.0) of requests in the last `window` that returned a
        5xx status code for this service.
        """
        total_query = (
            f'sum(rate(http_server_request_duration_seconds_count'
            f'{{service_name="{service_name}"}}[{window}]))'
        )
        error_query = (
            f'sum(rate(http_server_request_duration_seconds_count'
            f'{{service_name="{service_name}", http_response_status_code=~"5.."}}[{window}]))'
        )

        total_result = self.prom.query(total_query)
        error_result = self.prom.query(error_query)

        total = float(total_result[0]["value"][1]) if total_result else 0.0
        errors = float(error_result[0]["value"][1]) if error_result else 0.0

        return (errors / total) if total > 0 else 0.0

    def get_latency_percentile(
        self,
        service_name: str,
        percentile: float = 0.95,
        window: str = "5m",
    ) -> float:
        """
        Latency at the given percentile (e.g. 0.95 for p95), in seconds, for
        a service, aggregated across routes.
        """
        query = (
            f'histogram_quantile({percentile}, '
            f'sum(rate(http_server_request_duration_seconds_bucket'
            f'{{service_name="{service_name}"}}[{window}])) by (le))'
        )
        result = self.prom.query(query)
        if not result:
            return 0.0
        value = result[0]["value"][1]
        return float(value) if value != "NaN" else 0.0

    def get_latency_by_route(
        self,
        service_name: str,
        percentile: float = 0.95,
        window: str = "5m",
    ) -> Dict[str, float]:
        """
        Latency at the given percentile, broken down per route, for a service.
        """
        query = (
            f'histogram_quantile({percentile}, '
            f'sum(rate(http_server_request_duration_seconds_bucket'
            f'{{service_name="{service_name}"}}[{window}])) by (le, http_route))'
        )
        result = self.prom.query(query)
        latencies = {}
        for r in result:
            route = r["metric"].get("http_route", "unknown")
            value = r["value"][1]
            latencies[route] = float(value) if value != "NaN" else 0.0
        return latencies

    def get_cpu_usage(self, container_name: str, window: str = "5m") -> Optional[float]:
        """
        CPU utilization (fractional cores, e.g. 0.5 = half a core) for a
        container, computed as rate(container_cpu_usage_seconds_total[window])
        summed across the container's cgroup entries.

        There is no pre-computed utilization-ratio gauge in this cluster's
        cAdvisor output, so this is derived from the raw usage-seconds
        counter rather than read directly.
        """
        query = (
            f'sum(rate(container_cpu_usage_seconds_total'
            f'{{container="{container_name}"}}[{window}]))'
        )
        result = self.prom.query(query)
        return float(result[0]["value"][1]) if result else None

    def get_memory_usage(self, container_name: str) -> Optional[float]:
        """
        Current memory usage in bytes for a container.
        """
        query = f'container_memory_usage_bytes{{container="{container_name}"}}'
        result = self.prom.query(query)
        return float(result[0]["value"][1]) if result else None

    def get_top_cpu_consumers(self, limit: int = 5, window: str = "5m") -> List[Dict[str, Any]]:
        """
        Top N containers by current CPU utilization (fractional cores).
        """
        query = (
            f'topk({limit}, sum by (container) '
            f'(rate(container_cpu_usage_seconds_total{{container!=""}}[{window}])))'
        )
        result = self.prom.query(query)
        return [
            {
                "container_name": r["metric"].get("container", "unknown"),
                "cpu_utilization": float(r["value"][1]),
            }
            for r in result
        ]

    def get_service_summary(self, service_name: str, window: str = "5m") -> Dict[str, Any]:
        """
        Combined snapshot for a service: request rate, error rate, p95
        latency, and (if container_name matches service_name) CPU/memory.
        """
        return {
            "service_name": service_name,
            "request_rate_per_sec": self.get_request_rate(service_name, window),
            "error_rate": self.get_error_rate(service_name, window),
            "latency_p95_seconds": self.get_latency_percentile(service_name, 0.95, window),
            "cpu_utilization": self.get_cpu_usage(service_name),
            "memory_bytes": self.get_memory_usage(service_name),
        }


if __name__ == "__main__":
    agent = MetricsAgent(PrometheusClient("http://localhost:9090"))

    print("Top CPU consumers:")
    for c in agent.get_top_cpu_consumers():
        print(f"  {c['container_name']}: {c['cpu_utilization']:.3f}")

    print("\nShipping service summary:")
    summary = agent.get_service_summary("shipping")
    for k, v in summary.items():
        print(f"  {k}: {v}")