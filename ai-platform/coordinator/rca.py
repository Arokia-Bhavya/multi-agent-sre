"""
LLM-based root cause correlation and RCA report generation (Milestone 4).

The graph's `correlate_findings` node does plain, deterministic data-shaping
(trimming/aggregating raw agent output). This module is where the actual
*reasoning* happens: an LLM (via langchain-anthropic) reads the correlated
evidence and produces a structured root-cause finding, which is then
rendered into a markdown RCA report.

Requires ANTHROPIC_API_KEY to be set in the environment when the default
model is used.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RCAFinding(BaseModel):
    """Structured root-cause analysis produced by the LLM from correlated findings."""

    likely_root_cause: str = Field(
        description="One or two sentence best guess at the root cause of the incident."
    )
    confidence: str = Field(
        description='Confidence in the root cause: one of "low", "medium", "high".'
    )
    supporting_evidence: List[str] = Field(
        default_factory=list,
        description="Specific data points from the metrics/trace/Kubernetes findings that support the root cause.",
    )
    recommended_actions: List[str] = Field(
        default_factory=list,
        description="Concrete next troubleshooting or remediation steps for an on-call engineer.",
    )


RCA_SYSTEM_PROMPT = """You are an SRE incident investigator for the OpenTelemetry Demo \
(Astronomy Shop) platform. You are given a firing alert plus correlated evidence gathered \
by three specialist agents: a Metrics Agent (Prometheus), a Trace Agent (Jaeger), and a \
Kubernetes Agent.

Analyze the evidence and determine the most likely root cause of the incident.

Rules:
- Only use the evidence provided below; do not invent data points.
- If the evidence is thin or contradictory, say so explicitly and set confidence to "low" \
rather than guessing with unwarranted certainty.
- Prefer specific, actionable recommendations over generic advice."""


def build_rca_prompt(alert: Dict[str, Any], correlated_findings: Dict[str, Any]) -> str:
    """
    Render the alert and correlated findings into a prompt the LLM can reason over.
    """
    return (
        "## Firing Alert\n"
        f"{json.dumps(alert, indent=2, default=str)}\n\n"
        "## Correlated Findings\n"
        f"{json.dumps(correlated_findings, indent=2, default=str)}\n\n"
        "Determine the likely root cause."
    )


def render_markdown_report(
    alert: Dict[str, Any],
    service_name: Optional[str],
    correlated_findings: Dict[str, Any],
    rca: RCAFinding,
) -> str:
    """
    Render the final human-readable RCA report combining the LLM's structured
    finding with the raw evidence it was based on.
    """
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    evidence_lines = "\n".join(f"- {item}" for item in rca.supporting_evidence) or "- (none provided)"
    action_lines = "\n".join(f"- {item}" for item in rca.recommended_actions) or "- (none provided)"

    return f"""# Root Cause Analysis Report

**Generated:** {generated_at}
**Alert:** {alert.get("name", "unknown")} (severity: {alert.get("severity", "unknown")})
**Affected service:** {service_name or "unknown"}

## Likely Root Cause
{rca.likely_root_cause}

**Confidence:** {rca.confidence}

## Supporting Evidence
{evidence_lines}

## Recommended Actions
{action_lines}

## Raw Correlated Findings
```json
{json.dumps(correlated_findings, indent=2, default=str)}
```
"""
