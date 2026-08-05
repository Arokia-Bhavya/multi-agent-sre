"""
RCA/runbook eval fixtures.

Each case supplies an `alert` and `correlated_findings` shaped exactly like
`InvestigationGraph._correlate_findings`'s output (see
`ai_platform/coordinator/graph.py`), so `rca.build_rca_prompt` /
`runbook.build_runbook_prompt` can consume them unmodified — no live
Prometheus/Jaeger/Kubernetes needed, only the LLM call itself.

`root_cause_keywords`: list of keyword groups (each an OR of acceptable
phrasings); every group must have at least one hit somewhere in the RCA's
`likely_root_cause` + `supporting_evidence` combined. Keep groups loose
(synonyms, not exact phrases) since the LLM will paraphrase.

`expected_confidence`: set only on fixtures that deliberately test
calibration (thin/contradictory evidence -> must be "low"). Leave `None`
for fixtures with strong, unambiguous evidence, where confidence isn't the
point of the test.

`command_patterns` (runbook-only): regexes, at least one of which should
appear somewhere in the runbook's steps — checks that a concrete kubectl/
PromQL command shows up when the evidence makes one obvious.
"""

from typing import Any, Dict, List, Optional, Sequence


class RCACase:
    def __init__(
        self,
        case_id: str,
        alert: Dict[str, Any],
        correlated_findings: Dict[str, Any],
        root_cause_keywords: Sequence[Sequence[str]],
        expected_confidence: Optional[str] = None,
        command_patterns: Sequence[str] = (),
    ):
        self.case_id = case_id
        self.alert = alert
        self.correlated_findings = correlated_findings
        self.root_cause_keywords = root_cause_keywords
        self.expected_confidence = expected_confidence
        self.command_patterns = command_patterns


CASES: List[RCACase] = [
    RCACase(
        case_id="oom_crashloop_product_catalog",
        alert={
            "name": "ProductCatalogPodDown",
            "severity": "critical",
            "status": "firing",
            "labels": {"service_name": "product-catalog"},
            "annotations": {"summary": "product-catalog pod restarting repeatedly"},
        },
        correlated_findings={
            "service_name": "product-catalog",
            "metrics": {
                "request_rate": 0.4,
                "error_rate": 0.98,
                "p95_latency_seconds": 4.2,
                "cpu_utilization": 0.12,
                "memory_bytes": 512_000_000,
            },
            "slow_spans": [],
            "critical_path": [],
            "unhealthy_pods": [
                {"name": "product-catalog-7d9f6c9-abcde", "status": "CrashLoopBackOff", "restarts": 14}
            ],
            "high_restart_pods": [
                {"name": "product-catalog-7d9f6c9-abcde", "restarts": 14}
            ],
            "unhealthy_deployments": [
                {"name": "product-catalog", "desired_replicas": 2, "ready_replicas": 0}
            ],
            "warning_events": [
                {
                    "object": "product-catalog-7d9f6c9-abcde",
                    "reason": "OOMKilling",
                    "message": "Memory cgroup out of memory: Killed process product-catalog (limit 256Mi)",
                }
            ],
            "data_gaps": [],
        },
        root_cause_keywords=[
            ["oom", "out of memory", "oomkilled", "memory limit"],
        ],
        expected_confidence=None,  # strong single-cause evidence — don't require "low" here
        command_patterns=[r"kubectl (describe|logs|get)", r"kubectl set resources", r"kubectl edit"],
    ),
    RCACase(
        case_id="scaled_to_zero_checkout",
        alert={
            "name": "CheckoutUnavailable",
            "severity": "critical",
            "status": "firing",
            "labels": {"service_name": "checkout"},
            "annotations": {"summary": "checkout returning connection refused"},
        },
        correlated_findings={
            "service_name": "checkout",
            "metrics": {
                "request_rate": 0.0,
                "error_rate": 1.0,
                "p95_latency_seconds": 0.0,
                "cpu_utilization": 0.0,
                "memory_bytes": 0,
            },
            "slow_spans": [],
            "critical_path": [],
            "unhealthy_pods": [],
            "high_restart_pods": [],
            "unhealthy_deployments": [
                {"name": "checkout", "desired_replicas": 0, "ready_replicas": 0}
            ],
            "warning_events": [],
            "data_gaps": [],
        },
        root_cause_keywords=[
            [
                "scaled to zero", "scaled down", "zero replicas", "0 replicas", "0/0",
                "no replicas", "no ready", "0 ready", "0 desired", "desired replica",
                "replica count", "no running pods", "no pods", "unavailable",
            ],
        ],
        expected_confidence=None,
        command_patterns=[r"kubectl scale"],
    ),
    RCACase(
        case_id="slow_downstream_dependency_frontend",
        alert={
            "name": "FrontendHighLatency",
            "severity": "warning",
            "status": "firing",
            "labels": {"service_name": "frontend"},
            "annotations": {"summary": "frontend p95 latency above threshold"},
        },
        correlated_findings={
            "service_name": "frontend",
            "metrics": {
                "request_rate": 120.0,
                "error_rate": 0.01,
                "p95_latency_seconds": 3.8,
                "cpu_utilization": 0.35,
                "memory_bytes": 300_000_000,
            },
            "slow_spans": [
                {
                    "trace_id": "abc123",
                    "service": "recommendation",
                    "operation": "GetRecommendations",
                    "duration_ms": 3600,
                }
            ],
            "critical_path": [
                {"service": "frontend", "operation": "GET /", "duration_ms": 3800},
                {"service": "recommendation", "operation": "GetRecommendations", "duration_ms": 3600},
            ],
            "unhealthy_pods": [],
            "high_restart_pods": [],
            "unhealthy_deployments": [],
            "warning_events": [],
            "data_gaps": [],
        },
        root_cause_keywords=[
            ["recommendation"],
            ["slow", "latency", "bottleneck"],
        ],
        expected_confidence=None,
        command_patterns=[
            r"kubectl logs", r"kubectl describe", r"kubectl get", r"jaeger", r"trace", r"span",
            r"curl", r"promql", r"prometheus", r"recommendation",
        ],
    ),
    # --- Calibration case: evidence is genuinely thin. The RCA system
    # prompt explicitly requires confidence="low" and saying so explicitly
    # rather than guessing — this is the fixture that actually tests that.
    RCACase(
        case_id="thin_evidence_ambiguous_alert",
        alert={
            "name": "GenericServiceDegraded",
            "severity": "warning",
            "status": "firing",
            "labels": {"service_name": "currency"},
            "annotations": {"summary": "currency service flagged as degraded"},
        },
        correlated_findings={
            "service_name": "currency",
            "metrics": {},
            "slow_spans": [],
            "critical_path": [],
            "unhealthy_pods": [],
            "high_restart_pods": [],
            "unhealthy_deployments": [],
            "warning_events": [],
            "data_gaps": ["metrics agent failed: connection timeout", "trace agent failed: connection timeout"],
        },
        root_cause_keywords=[
            [
                "insufficient", "thin", "unclear", "unable to determine", "not enough evidence",
                "no data", "limited evidence", "sparse", "lack of", "lacking", "cannot determine",
                "cannot pinpoint", "cannot conclusively", "no conclusive", "connection timeout",
                "agents failed", "failed to gather", "no visibility", "could not be gathered",
            ],
        ],
        expected_confidence="low",
        command_patterns=[r"check", r"verify", r"investigate"],
    ),
]
