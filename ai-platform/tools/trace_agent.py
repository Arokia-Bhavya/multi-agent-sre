"""
Trace Agent for Multi-Agent SRE Platform

Answers questions about distributed traces for services in the OpenTelemetry
Demo, using Jaeger's trace/span data: retrieving traces, finding slow spans,
and identifying the critical path through a trace (the chain of spans that
determines overall trace latency).
"""

from typing import Dict, List, Any, Optional

from jaeger_client import JaegerClient


class TraceAgent:
    """
    Agent responsible for answering tracing-related questions using
    Jaeger data from the OpenTelemetry Demo.

    Example:
        agent = TraceAgent(JaegerClient("http://localhost:16686"))
        agent.get_traces("checkout")
        agent.get_slow_spans("checkout", min_duration_ms=200)
        agent.get_critical_path(trace_id)
    """

    def __init__(self, jaeger_client: JaegerClient):
        self.jaeger = jaeger_client

    def get_traces(
        self,
        service: str,
        operation: Optional[str] = None,
        limit: int = 20,
        lookback: str = "1h",
    ) -> List[Dict[str, Any]]:
        """
        Retrieve recent traces for a service.
        """
        return self.jaeger.find_traces(service, operation=operation, limit=limit, lookback=lookback)

    def get_slow_spans(
        self,
        service: str,
        min_duration_ms: float = 100,
        limit: int = 20,
        trace_limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Find individual spans (not whole traces) that exceed a duration
        threshold, across recent traces for a service.

        Returns:
            List of span summaries sorted by duration (descending), each with
            trace_id, span_id, service, operation, and duration_ms.
        """
        traces = self.jaeger.find_traces(service, limit=trace_limit)
        slow_spans = []
        for trace in traces:
            processes = trace.get("processes", {})
            for span in trace.get("spans", []):
                duration_ms = span.get("duration", 0) / 1000
                if duration_ms >= min_duration_ms:
                    process = processes.get(span.get("processID"), {})
                    slow_spans.append({
                        "trace_id": trace.get("traceID"),
                        "span_id": span.get("spanID"),
                        "service": process.get("serviceName", "unknown"),
                        "operation": span.get("operationName", "unknown"),
                        "duration_ms": duration_ms,
                    })

        slow_spans.sort(key=lambda s: s["duration_ms"], reverse=True)
        return slow_spans[:limit]

    def get_critical_path(self, trace_id: str) -> List[Dict[str, Any]]:
        """
        Determine the critical path through a trace: the chain of spans from
        the root span to the last span to finish, following at each step the
        child span that finishes latest. This is the chain of work that
        actually determines the trace's overall latency.

        Returns:
            Ordered list of span summaries (root first) with service,
            operation, span_id, and duration_ms.
        """
        trace = self.jaeger.get_trace(trace_id)
        spans = trace.get("spans", [])
        if not spans:
            return []

        processes = trace.get("processes", {})
        span_by_id = {span["spanID"]: span for span in spans}
        children: Dict[str, List[Dict[str, Any]]] = {}
        roots = []

        for span in spans:
            parent_id = next(
                (ref.get("spanID") for ref in span.get("references", [])
                 if ref.get("refType") == "CHILD_OF" and ref.get("spanID") in span_by_id),
                None,
            )
            if parent_id:
                children.setdefault(parent_id, []).append(span)
            else:
                roots.append(span)

        if not roots:
            roots = [spans[0]]

        def end_time(span: Dict[str, Any]) -> int:
            return span.get("startTime", 0) + span.get("duration", 0)

        def to_summary(span: Dict[str, Any]) -> Dict[str, Any]:
            process = processes.get(span.get("processID"), {})
            return {
                "span_id": span.get("spanID"),
                "service": process.get("serviceName", "unknown"),
                "operation": span.get("operationName", "unknown"),
                "duration_ms": span.get("duration", 0) / 1000,
            }

        current = max(roots, key=end_time)
        path = [to_summary(current)]
        while True:
            kids = children.get(current["spanID"], [])
            if not kids:
                break
            current = max(kids, key=end_time)
            path.append(to_summary(current))

        return path

    def get_trace_summary(self, trace_id: str) -> Dict[str, Any]:
        """
        Combined snapshot for a trace: total duration, span count, and its
        critical path.
        """
        trace = self.jaeger.get_trace(trace_id)
        spans = trace.get("spans", [])
        if not spans:
            return {"trace_id": trace_id, "found": False}

        starts = [s.get("startTime", 0) for s in spans]
        ends = [s.get("startTime", 0) + s.get("duration", 0) for s in spans]

        return {
            "trace_id": trace_id,
            "found": True,
            "span_count": len(spans),
            "duration_ms": (max(ends) - min(starts)) / 1000,
            "critical_path": self.get_critical_path(trace_id),
        }


if __name__ == "__main__":
    agent = TraceAgent(JaegerClient("http://localhost:16686"))

    print("Slow spans for 'checkout' (>100ms):")
    for span in agent.get_slow_spans("checkout", min_duration_ms=100):
        print(f"  {span['service']}.{span['operation']}: {span['duration_ms']:.1f}ms")

    traces = agent.get_traces("checkout", limit=1)
    if traces:
        summary = agent.get_trace_summary(traces[0]["traceID"])
        print(f"\nTrace summary: {summary}")
