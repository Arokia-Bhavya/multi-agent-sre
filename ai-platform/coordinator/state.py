"""
State schema for the SRE incident investigation graph (Milestone 4).
"""

from typing import Any, Dict, List, Optional, TypedDict


class InvestigationState(TypedDict, total=False):
    """
    State threaded through the LangGraph investigation workflow.

    Only `alert` (and optionally `known_services`) needs to be supplied as
    input; every other key is populated by a node as the investigation
    proceeds.
    """

    # Input
    alert: Dict[str, Any]
    known_services: List[str]

    # Populated by receive_alert
    service_name: Optional[str]
    errors: List[str]

    # Populated by the parallel agent-invocation nodes
    metrics_findings: Dict[str, Any]
    trace_findings: Dict[str, Any]
    kubernetes_findings: Dict[str, Any]

    # Populated by correlate_findings
    correlated_findings: Dict[str, Any]

    # Populated by produce_rca_report
    rca_finding: Any  # RCAFinding (Any here to avoid state.py depending on rca.py)
    rca_report: str

    # Populated on demand by InvestigationGraph.generate_runbook (Milestone 6),
    # not by the graph itself — investigate() alone never sets these.
    runbook: Any  # RunbookDoc
    runbook_report: str
