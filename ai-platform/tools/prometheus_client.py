"""
Prometheus Client for Multi-Agent SRE Platform

Provides methods to query Prometheus metrics, retrieve alert rules, and analyze 
time-series data from the monitoring system.
"""

import requests
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta


class PrometheusClient:
    """
    Client for querying Prometheus metrics and alert information.
    
    Example:
        client = PrometheusClient("http://localhost:9090")
        memory_usage = client.query("container_memory_usage_bytes")
        cpu_rate = client.query_range(
            "rate(container_cpu_usage_seconds_total[5m])",
            start=datetime.now() - timedelta(hours=1),
            end=datetime.now()
        )
    """
    
    def __init__(self, prometheus_url: str = "http://localhost:9090", timeout: float = 15):
        """
        Initialize Prometheus client.
        
        Args:
            prometheus_url: Base URL of Prometheus server (default: localhost:9090)
        """
        self.base_url = prometheus_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get_data(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Request a Prometheus API endpoint and return its successful payload."""
        response = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus API request failed: {data.get('error', 'unknown error')}")
        return data
    
    def query(self, query: str) -> List[Dict[str, Any]]:
        """
        Execute an instant query at the current time.
        
        Args:
            query: PromQL query string
            
        Returns:
            List of result dicts with 'metric' and 'value' keys
            
        Example:
            >>> client.query("up{namespace='otel-demo'}")
            [
                {
                    'metric': {'__name__': 'up', 'namespace': 'otel-demo', ...},
                    'value': [1721892000, '1']
                }
            ]
        """
        params = {"query": query}
        return self._get_data("/api/v1/query", params).get("data", {}).get("result", [])
    
    def query_range(
        self, 
        query: str, 
        start: datetime, 
        end: datetime, 
        step: str = "1m"
    ) -> List[Dict[str, Any]]:
        """
        Execute a range query over time.
        
        Args:
            query: PromQL query string
            start: Start timestamp
            end: End timestamp
            step: Query resolution step (default: 1m)
            
        Returns:
            List of result dicts with 'metric' and 'values' (time series array)
            
        Example:
            >>> client.query_range(
            ...     "container_memory_usage_bytes{namespace='otel-demo'}",
            ...     start=datetime.now() - timedelta(hours=1),
            ...     end=datetime.now(),
            ...     step="5m"
            ... )
        """
        params = {
            "query": query,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "step": step,
        }
        return self._get_data("/api/v1/query_range", params).get("data", {}).get("result", [])
    
    def get_alerts(self) -> List[Dict[str, Any]]:
        """
        Retrieve all active alerts from Prometheus.
        
        Returns:
            List of active alert dicts with labels, state, and value
            
        Example:
            >>> alerts = client.get_alerts()
            >>> for alert in alerts:
            ...     print(f"{alert['labels']['alertname']}: {alert['state']}")
        """
        return self._get_data("/api/v1/alerts").get("data", {}).get("alerts", [])
    
    def get_alert_rules(self) -> List[Dict[str, Any]]:
        """
        Retrieve all alert rules from Prometheus.
        
        Returns:
            List of alert rule dicts with name, expr, and state
            
        Example:
            >>> rules = client.get_alert_rules()
            >>> for rule in rules:
            ...     print(f"{rule['name']}: {rule['state']}")
        """
        data = self._get_data("/api/v1/rules")
        rules = []
        for group in data.get("data", {}).get("groups", []):
            rules.extend(rule for rule in group.get("rules", []) if rule.get("type") == "alerting")
        return rules
    
    def get_metric_names(self) -> List[str]:
        """
        Retrieve all available metric names.
        
        Returns:
            List of metric name strings
            
        Example:
            >>> metrics = client.get_metric_names()
            >>> print(f"Available metrics: {len(metrics)}")
        """
        return self._get_data("/api/v1/label/__name__/values").get("data", [])
    
    def get_pod_memory_usage(
        self, 
        namespace: str, 
        duration: str = "5m"
    ) -> Dict[str, str]:
        """
        Get current memory usage for all pods in a namespace.
        
        Args:
            namespace: Kubernetes namespace
            duration: Time duration for average (default: 5m)
            
        Returns:
            Dict mapping pod name to memory usage (bytes)
            
        Example:
            >>> memory = client.get_pod_memory_usage("otel-demo")
            >>> for pod, usage_bytes in memory.items():
            ...     usage_mb = int(usage_bytes) / (1024 * 1024)
            ...     print(f"{pod}: {usage_mb:.2f} MB")
        """
        query = (
            f"avg(avg_over_time(container_memory_usage_bytes{{namespace='{namespace}',"
            f"container!=''}}[{duration}])) by (pod)"
        )
        results = self.query(query)
        
        memory_data = {}
        for result in results:
            pod_name = result["metric"].get("pod", "unknown")
            memory_value = result["value"][1]
            memory_data[pod_name] = memory_value
        
        return memory_data
    
    def get_pod_cpu_rate(
        self, 
        namespace: str, 
        duration: str = "5m"
    ) -> Dict[str, str]:
        """
        Get current CPU usage rate for all pods in a namespace.
        
        Args:
            namespace: Kubernetes namespace
            duration: Duration for rate calculation (default: 5m)
            
        Returns:
            Dict mapping pod name to CPU rate (fraction of cores)
            
        Example:
            >>> cpu = client.get_pod_cpu_rate("otel-demo")
            >>> for pod, rate in cpu.items():
            ...     print(f"{pod}: {float(rate):.3f} cores")
        """
        query = (
            f"sum(rate(container_cpu_usage_seconds_total{{namespace='{namespace}',"
            f"container!=''}}[{duration}])) by (pod)"
        )
        results = self.query(query)
        
        cpu_data = {}
        for result in results:
            pod_name = result["metric"].get("pod", "unknown")
            cpu_value = result["value"][1]
            cpu_data[pod_name] = cpu_value
        
        return cpu_data
    
    def get_pod_network_io(
        self, 
        namespace: str
    ) -> Dict[str, Dict[str, str]]:
        """
        Get network I/O statistics for pods in a namespace.
        
        Args:
            namespace: Kubernetes namespace
            
        Returns:
            Dict mapping pod name to {'bytes_sent', 'bytes_recv'}
            
        Example:
            >>> io = client.get_pod_network_io("otel-demo")
            >>> for pod, stats in io.items():
            ...     print(f"{pod}: {stats['bytes_sent']} sent, {stats['bytes_recv']} recv")
        """
        sent_query = f"sum(container_network_transmit_bytes_total{{namespace='{namespace}'}}) by (pod)"
        recv_query = f"sum(container_network_receive_bytes_total{{namespace='{namespace}'}}) by (pod)"
        
        sent_results = self.query(sent_query)
        recv_results = self.query(recv_query)
        
        sent_data = {r["metric"].get("pod"): r["value"][1] for r in sent_results}
        recv_data = {r["metric"].get("pod"): r["value"][1] for r in recv_results}
        
        io_data = {}
        for pod in set(sent_data.keys()) | set(recv_data.keys()):
            io_data[pod] = {
                "bytes_sent": sent_data.get(pod, "0"),
                "bytes_recv": recv_data.get(pod, "0"),
            }
        
        return io_data
