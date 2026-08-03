"""
Anomaly Detector for Multi-Agent SRE Platform (Milestone 6).

Flags services whose current CPU, memory, latency, or request-rate metric is
statistically unusual relative to its *own* recent history — a z-score check
over a Prometheus range query — as a complement to the static
threshold-based alert rules from Milestone 1 (which only fire once a fixed
threshold like ">80% CPU" is crossed, and say nothing about metrics that are
merely behaving unusually for that particular service).
"""

from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List

from prometheus_client import PrometheusClient


class AnomalyDetector:
    """
    Detects statistical anomalies in Prometheus metrics for OTel Demo services.

    Example:
        detector = AnomalyDetector(PrometheusClient("http://localhost:9090"))
        detector.detect_cpu_anomaly("checkout")
        detector.scan_service("checkout")
    """

    def __init__(self, prometheus_client: PrometheusClient, z_score_threshold: float = 3.0):
        """
        Args:
            prometheus_client: Client to source metric history from.
            z_score_threshold: Absolute z-score above which the latest data
                point is flagged as anomalous (default 3.0 — the "3-sigma"
                rule of thumb for a roughly-normal distribution).
        """
        self.prom = prometheus_client
        self.z_score_threshold = z_score_threshold

    def _detect_for_query(
        self, query: str, lookback_minutes: int = 60, step: str = "1m"
    ) -> Dict[str, Any]:
        """
        Fetch a metric's recent history, treat the most recent data point as
        the "current" value and every earlier point as the baseline, and
        flag the current value if it's more than `z_score_threshold` standard
        deviations from the baseline mean.
        """
        end = datetime.now()
        start = end - timedelta(minutes=lookback_minutes)
        result = self.prom.query_range(query, start=start, end=end, step=step)

        values: List[float] = []
        for series in result:
            for _, raw in series.get("values", []):
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    continue
                if v == v:  # filters out NaN (NaN != NaN)
                    values.append(v)

        if len(values) < 2:
            return {
                "query": query,
                "is_anomaly": False,
                "reason": "insufficient history",
                "sample_size": len(values),
            }

        current = values[-1]
        baseline = values[:-1]
        mu = mean(baseline)
        sigma = pstdev(baseline)
        if sigma > 0:
            z_score = (current - mu) / sigma
            is_anomaly = abs(z_score) >= self.z_score_threshold
        else:
            # A perfectly flat baseline has no variance to compute a z-score
            # against; any deviation at all from that constant is unusual.
            is_anomaly = current != mu
            z_score = self.z_score_threshold if is_anomaly else 0.0

        return {
            "query": query,
            "current_value": current,
            "baseline_mean": mu,
            "baseline_stddev": sigma,
            "z_score": z_score,
            "is_anomaly": is_anomaly,
            "sample_size": len(baseline),
        }

    def detect_cpu_anomaly(self, container_name: str, lookback_minutes: int = 60) -> Dict[str, Any]:
        """Flag anomalous CPU utilization for a container vs. its own recent history."""
        query = f'container_cpu_utilization_ratio{{container_name="{container_name}"}}'
        result = self._detect_for_query(query, lookback_minutes)
        result["metric"] = "cpu_utilization"
        result["container_name"] = container_name
        return result

    def detect_memory_anomaly(self, container_name: str, lookback_minutes: int = 60) -> Dict[str, Any]:
        """Flag anomalous memory usage for a container vs. its own recent history."""
        query = f'container_memory_usage_total_bytes{{container_name="{container_name}"}}'
        result = self._detect_for_query(query, lookback_minutes)
        result["metric"] = "memory_bytes"
        result["container_name"] = container_name
        return result

    def detect_latency_anomaly(
        self,
        service_name: str,
        percentile: float = 0.95,
        window: str = "5m",
        lookback_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Flag anomalous p{percentile} latency for a service vs. its own recent history."""
        query = (
            f'histogram_quantile({percentile}, '
            f'sum(rate(http_server_request_duration_seconds_bucket'
            f'{{service_name="{service_name}"}}[{window}])) by (le))'
        )
        result = self._detect_for_query(query, lookback_minutes)
        result["metric"] = "latency_seconds"
        result["service_name"] = service_name
        return result

    def detect_request_rate_anomaly(
        self, service_name: str, window: str = "5m", lookback_minutes: int = 60
    ) -> Dict[str, Any]:
        """
        Flag anomalous request rate for a service vs. its own recent history.
        Catches both traffic spikes and a drop-to-near-zero (e.g. upstream
        routing failure) that a simple ">threshold" alert rule would miss.
        """
        query = (
            f'sum(rate(http_server_request_duration_seconds_count'
            f'{{service_name="{service_name}"}}[{window}]))'
        )
        result = self._detect_for_query(query, lookback_minutes)
        result["metric"] = "request_rate_per_sec"
        result["service_name"] = service_name
        return result

    def scan_service(self, service_name: str, lookback_minutes: int = 60) -> Dict[str, Any]:
        """
        Run all anomaly checks for a service (CPU, memory, latency, request
        rate) and return a combined report with an overall has_anomaly flag.
        """
        checks = {
            "cpu": self.detect_cpu_anomaly(service_name, lookback_minutes),
            "memory": self.detect_memory_anomaly(service_name, lookback_minutes),
            "latency": self.detect_latency_anomaly(service_name, lookback_minutes=lookback_minutes),
            "request_rate": self.detect_request_rate_anomaly(
                service_name, lookback_minutes=lookback_minutes
            ),
        }
        return {
            "service_name": service_name,
            "checks": checks,
            "has_anomaly": any(c.get("is_anomaly") for c in checks.values()),
        }


if __name__ == "__main__":
    detector = AnomalyDetector(PrometheusClient("http://localhost:9090"))
    print(detector.scan_service("checkout"))
