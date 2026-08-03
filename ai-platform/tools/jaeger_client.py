"""
Jaeger Client for Multi-Agent SRE Platform

Provides methods to query traces, retrieve spans, and analyze distributed tracing data.
"""

import requests
from typing import Dict, List, Any, Optional
from datetime import datetime


class JaegerClient:
    """
    Client for querying Jaeger traces and analyzing spans.
    
    Example:
        client = JaegerClient("http://localhost:16686")
        traces = client.find_traces(service="frontend", operation="GET /")
    """
    
    def __init__(
        self,
        jaeger_url: str = "http://localhost:16686",
        timeout: float = 15,
        api_base_path: Optional[str] = None,
    ):
        """
        Initialize Jaeger client.
        
        Args:
            jaeger_url: Base URL of Jaeger server (default: localhost:16686)
            timeout: HTTP request timeout in seconds
            api_base_path: Optional Jaeger query API base path. Leave unset to
                support both a standard Jaeger deployment and the OpenTelemetry
                Demo configuration, which uses ``/jaeger/ui``.
        """
        self.base_url = jaeger_url.rstrip("/")
        self.timeout = timeout
        self.api_base_path = api_base_path.rstrip("/") if api_base_path else None
        self.session = requests.Session()

    def _get_data(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Get a Jaeger query endpoint, including the Demo chart base-path fallback."""
        base_paths = [self.api_base_path] if self.api_base_path is not None else ["", "/jaeger/ui"]
        response = None
        for base_path in base_paths:
            response = self.session.get(
                f"{self.base_url}{base_path}{path}", params=params, timeout=self.timeout
            )
            if response.status_code != 404:
                break
        assert response is not None
        response.raise_for_status()
        data = response.json()
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"Jaeger API request failed: {data['errors']}")
        return data.get("data", [])
    
    def get_services(self) -> List[str]:
        """
        Retrieve all available services in Jaeger.
        
        Returns:
            List of service names
        """
        return self._get_data("/api/services")
    
    def find_traces(
        self,
        service: str,
        operation: Optional[str] = None,
        limit: int = 20,
        lookback: str = "1h"
    ) -> List[Dict[str, Any]]:
        """
        Find traces for a given service and optional operation.
        
        Args:
            service: Service name
            operation: Operation name (optional)
            limit: Maximum number of traces to return
            lookback: How far back to search (default: 1h)
            
        Returns:
            List of trace dicts
        """
        params = {
            "service": service,
            "limit": limit,
            "lookback": lookback,
        }
        if operation:
            params["operation"] = operation
        
        return self._get_data("/api/traces", params)
    
    def get_trace(self, trace_id: str) -> Dict[str, Any]:
        """
        Retrieve detailed information about a specific trace.
        
        Args:
            trace_id: Trace ID
            
        Returns:
            Trace dict with all spans and timing information
        """
        traces = self._get_data(f"/api/traces/{trace_id}")
        return traces[0] if traces else {}
    
    def find_slow_traces(
        self,
        service: str,
        min_duration_ms: int = 1000,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Find traces that exceed a minimum duration threshold.
        
        Args:
            service: Service name
            min_duration_ms: Minimum duration in milliseconds
            limit: Maximum number of traces
            
        Returns:
            List of slow trace dicts
        """
        traces = self.find_traces(service, limit=limit * 3)
        def trace_duration_us(trace: Dict[str, Any]) -> int:
            if "duration" in trace:
                return int(trace["duration"])
            spans = trace.get("spans", [])
            if not spans:
                return 0
            starts = [span.get("startTime", 0) for span in spans]
            ends = [span.get("startTime", 0) + span.get("duration", 0) for span in spans]
            return max(ends) - min(starts)

        slow_traces = [t for t in traces if trace_duration_us(t) >= min_duration_ms * 1000]
        return slow_traces[:limit]
