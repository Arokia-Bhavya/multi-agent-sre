"""
Auto-triage of firing alerts (Milestone 6 extension).

Closes the loop the rest of the platform left open: everything through
Milestone 5 is pull-based (a human has to ask the copilot a question, or
open the web UI's alert panel, before anything happens). This module makes
alerts push-based instead — an Alertmanager webhook lands here, and a new
firing alert is picked up and investigated automatically, with no human
having to notice it first.

Flow:
    Alertmanager fires -> POST /api/webhook/alertmanager (see server.py)
        -> handle_alertmanager_webhook() parses + dedups the payload
        -> investigate_incident() runs in the background for each new alert:
             copilot.ask("investigate <service> for <alertname>...")
        -> result (RCA report, and a pending remediation approval if the
           copilot proposed one) is stored in IncidentStore
        -> the web UI's Incidents panel polls GET /api/incidents and shows
           it; a human still has to approve/reject any remediation via the
           existing /api/approve endpoint — this only automates the
           "notice + investigate" step, never the "change the cluster" step.

Kept separate from server.py so the dedup/parsing/prompt-building logic is
unit-testable without spinning up FastAPI or a real copilot.

`IncidentRecord`/`IncidentStore` themselves now live in
`ai-platform/tools/incident_store.py` (SQLite-backed, so incident/RCA
history survives a restart and is queryable by the copilot itself via
`get_incident_history` — see that module's docstring) and are re-exported
here for backward compatibility, since `server.py` and the existing tests
already import them from this module.
"""

import sys
import os
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coordinator"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from graph import extract_service_name  # noqa: E402
from incident_store import IncidentRecord, IncidentStore, DEFAULT_DB_PATH  # noqa: E402,F401


def _build_investigation_prompt(alert_name: str, service: Optional[str], severity: str, description: str) -> str:
    target = f"the {service} service" if service else "the affected service (see alert labels below)"
    context = f"\n\nAlert details: {description}" if description else ""
    return (
        f"An alert just fired: {alert_name} (severity: {severity}) on {target}. "
        f"Investigate the root cause. If the evidence clearly supports a concrete, "
        f"low-risk fix, propose it — otherwise just report findings and recommended "
        f"next steps.{context}"
    )


def handle_alertmanager_webhook(
    payload: Dict[str, Any], store: IncidentStore, known_services: Optional[List[str]] = None
) -> List[str]:
    """
    Parse an Alertmanager webhook payload (the standard
    `{"alerts": [{"status", "labels", "annotations", "fingerprint", ...}]}`
    shape) and register any new firing alerts in `store`.

    Returns the list of fingerprints that need investigation kicked off
    (i.e. newly-seen firing alerts) — the caller (server.py) is responsible
    for actually running that investigation, e.g. as a FastAPI background
    task, so this function stays synchronous and cheap to unit-test.

    Resolved alerts are marked `status="resolved"` on their existing record
    (if any). An alert fingerprint that's already tracked and still open
    (not resolved) is left alone — we don't re-trigger investigation on
    every Alertmanager repeat-interval resend of the same firing alert. But
    a fingerprint whose previous record is `resolved` is treated as a new
    incident when it fires again: Alertmanager's fingerprint is a hash of
    the alert's label set, so a recurring alert (same service/alertname)
    reuses the same fingerprint across separate occurrences, and without
    this check a second, unrelated firing would be silently dropped.
    """
    to_investigate: List[str] = []

    for alert in payload.get("alerts", []) or []:
        fingerprint = alert.get("fingerprint") or f"{alert.get('labels', {}).get('alertname', 'unknown')}:{alert.get('startsAt', '')}"
        status = (alert.get("status") or "firing").lower()
        labels = alert.get("labels", {}) or {}
        annotations = alert.get("annotations", {}) or {}
        alert_name = labels.get("alertname", "UnknownAlert")

        existing = store.get(fingerprint)

        if status == "resolved":
            if existing is not None and existing.status != "resolved":
                store.update(fingerprint, status="resolved")
            continue

        if existing is not None and existing.status != "resolved":
            # Already tracked and still open (investigating, awaiting
            # approval, completed, or errored) — don't restart investigation
            # on a repeat notification of the same ongoing incident.
            continue
        # existing is None, or its fingerprint previously resolved and is now
        # firing again — Alertmanager reuses the same fingerprint (a hash of
        # the label set) for every occurrence of the same alert, so a
        # resolved incident re-firing later looks identical to a repeat
        # notification unless we check status. Treat it as a new incident:
        # store.create() upserts on fingerprint and mints a fresh thread_id.

        service = extract_service_name({"labels": labels, "name": alert_name}, known_services=known_services)
        severity = labels.get("severity", "unknown")
        store.create(fingerprint, alert_name=alert_name, service=service, severity=severity)
        to_investigate.append(fingerprint)

    return to_investigate


def investigate_incident(copilot: Any, store: IncidentStore, fingerprint: str) -> None:
    """
    Run the copilot's investigation for one incident and record the outcome.
    Meant to be called off the request thread (e.g. via FastAPI
    `BackgroundTasks`) since a full investigation is several LLM/tool round
    trips and shouldn't block the webhook response back to Alertmanager.
    """
    record = store.get(fingerprint)
    if record is None:
        return

    annotations_desc = ""  # kept simple; annotations aren't persisted on the record today
    prompt = _build_investigation_prompt(record.alert_name, record.service, record.severity, annotations_desc)

    try:
        reply = copilot.ask(prompt, thread_id=record.thread_id)
    except Exception as exc:  # keep the incident visible with the failure, don't drop it silently
        store.update(fingerprint, status="error", error=str(exc))
        return

    status = "awaiting_approval" if reply.pending_action else "completed"
    store.update(
        fingerprint,
        status=status,
        content=reply.content,
        steps=reply.steps,
        pending_action=reply.pending_action,
    )


def record_approval_outcome(store: IncidentStore, thread_id: str, reply: Any) -> None:
    """
    After a human approves/rejects a pending remediation for an
    auto-triaged incident (via the existing /api/approve endpoint), update
    that incident's record so the Incidents panel reflects the outcome
    instead of staying stuck on "awaiting approval".
    """
    record = store.get_by_thread(thread_id)
    if record is None:
        return
    status = "awaiting_approval" if reply.pending_action else "completed"
    store.update(
        record.fingerprint,
        status=status,
        content=reply.content,
        steps=reply.steps,
        pending_action=reply.pending_action,
    )
