"""
AI-generated remediation runbooks (Milestone 6).

Takes the structured RCAFinding an investigation already produced (see
rca.py) and asks the LLM for a second, more execution-focused structured
output: a step-by-step runbook an on-call engineer could follow directly,
including concrete kubectl/PromQL commands where the evidence supports one,
rather than the RCA's shorter "recommended_actions" bullet list.

Kept as a second LLM call rather than folded into RCAFinding itself so the
RCA step stays focused on diagnosis (what's wrong) and this step stays
focused on remediation (what to do about it) — callers that only want the
former (e.g. the existing investigate_service tool) don't pay for the
latter's extra round trip.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from rca import RCAFinding


class RunbookDoc(BaseModel):
    """Structured, execution-ready remediation runbook produced by the LLM."""

    title: str = Field(description="Short, specific title, e.g. 'Recover product-catalog from 0 replicas'.")
    summary: str = Field(description="One or two sentence summary of what happened and what this runbook fixes.")
    prerequisites: List[str] = Field(
        default_factory=list,
        description="Things the engineer needs before starting (access, tools, confirmations).",
    )
    steps: List[str] = Field(
        description=(
            "Ordered, concrete remediation steps. Include an actual kubectl/PromQL "
            "command in each step where the evidence makes one obvious (e.g. the exact "
            "deployment name and namespace), not generic advice."
        )
    )
    verification_steps: List[str] = Field(
        default_factory=list,
        description="How to confirm the fix worked (specific commands/queries and what output to expect).",
    )
    rollback_steps: List[str] = Field(
        default_factory=list,
        description="How to undo this runbook's changes if it makes things worse.",
    )


RUNBOOK_SYSTEM_PROMPT = """You are an SRE writing a remediation runbook for the OpenTelemetry Demo \
(Astronomy Shop) platform, Kubernetes namespace "otel-demo". You are given a firing alert, the \
correlated evidence gathered by the investigation, and the root-cause analysis already produced \
from that evidence.

Write a runbook another on-call engineer could follow directly during an incident, without needing \
to re-derive anything.

Rules:
- Ground every step in the root cause and evidence given — do not invent services, deployments, or \
data points that aren't in the evidence.
- Prefer a concrete command (kubectl, curl against Prometheus, etc.) using the actual names from the \
evidence over a vague instruction like "check the deployment".
- Order steps the way an engineer would actually execute them: diagnose/confirm first if not already \
certain, then remediate, then verify.
- If the root cause's confidence is "low", say so in the summary and make the first step further \
diagnosis rather than jumping straight to a fix."""


def build_runbook_prompt(
    alert: Dict[str, Any],
    service_name: Optional[str],
    correlated_findings: Dict[str, Any],
    rca: RCAFinding,
) -> str:
    return (
        "## Firing Alert\n"
        f"{json.dumps(alert, indent=2, default=str)}\n\n"
        f"## Affected Service\n{service_name or 'unknown'}\n\n"
        "## Root Cause Analysis\n"
        f"Likely root cause: {rca.likely_root_cause}\n"
        f"Confidence: {rca.confidence}\n"
        f"Supporting evidence: {rca.supporting_evidence}\n\n"
        "## Correlated Findings (raw evidence)\n"
        f"{json.dumps(correlated_findings, indent=2, default=str)}\n\n"
        "Write the remediation runbook."
    )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "runbook"


def render_runbook_markdown(
    alert: Dict[str, Any],
    service_name: Optional[str],
    rca: RCAFinding,
    runbook: RunbookDoc,
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _list(items: List[str], empty: str) -> str:
        return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items)) if items else empty

    def _bullets(items: List[str], empty: str) -> str:
        return "\n".join(f"- {item}" for item in items) if items else empty

    return f"""# {runbook.title}

**Generated:** {generated_at}
**Alert:** {alert.get("name", "unknown")} (severity: {alert.get("severity", "unknown")})
**Affected service:** {service_name or "unknown"}

## Summary
{runbook.summary}

**Root cause (confidence: {rca.confidence}):** {rca.likely_root_cause}

## Prerequisites
{_bullets(runbook.prerequisites, "- None")}

## Remediation Steps
{_list(runbook.steps, "1. (none generated)")}

## Verification
{_list(runbook.verification_steps, "1. (none generated)")}

## Rollback
{_bullets(runbook.rollback_steps, "- (none generated)")}
"""


def runbook_filename(service_name: Optional[str], alert_name: Optional[str]) -> str:
    """Filesystem-safe filename for saving a generated runbook, e.g.
    `product-catalog_productcatalogpoddown_20260729T161200Z.md`."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    service_slug = _slugify(service_name or "unknown-service")
    alert_slug = _slugify(alert_name or "manual")
    return f"{service_slug}_{alert_slug}_{timestamp}.md"
