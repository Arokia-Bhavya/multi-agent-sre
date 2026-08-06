"""
Web UI backend for the AI SRE Copilot (Milestone 6).

A thin FastAPI wrapper around the same `SRECopilot` used by `chat.py`'s
terminal REPL, so the browser gets the identical tool set, conversation
memory, and human-in-the-loop remediation approval flow — just over HTTP
instead of stdin/stdout.

Requires the same environment as chat.py (see .env.example):
    - ANTHROPIC_API_KEY (default provider), or LLM_PROVIDER=groq + GROQ_API_KEY
    - Prometheus and Jaeger port-forwarded
    - A working kubeconfig context for the target cluster
    - fastapi, uvicorn (pip/uv install fastapi uvicorn)

Run:
    uv run python ai-platform/webui/server.py

Then open http://localhost:8000
"""

import os
import sys
import uuid
from typing import Any, Dict, List, Optional


from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_platform.copilot.chat import build_copilot  # reuses the exact same agent/tool wiring as the CLI
from ai_platform.copilot.agent import SRECopilot, CopilotReply
from ai_platform.webui.auto_responder import (
    DEFAULT_DB_PATH,
    IncidentStore,
    handle_alertmanager_webhook,
    investigate_incident,
    record_approval_outcome,
)


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="AI SRE Copilot Web UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Auto-triage registry (Milestone 6 extension): alerts pushed in via
# /api/webhook/alertmanager land here, keyed by Alertmanager fingerprint, so
# the Incidents panel can show what the copilot picked up on its own without
# a human having to ask first. Backed by SQLite (DEFAULT_DB_PATH) rather than
# ":memory:" so incident/RCA history survives a restart of this process and
# is queryable by the copilot itself (get_incident_history).
_incidents = IncidentStore(DEFAULT_DB_PATH)

# A single shared SRECopilot instance, mirroring chat.py's model: one process,
# one set of live agent connections (Prometheus/Jaeger/K8s clients), with
# per-browser-session conversation memory scoped by thread_id (the
# checkpointer already keys on thread_id, so this needs no extra locking
# beyond what LangGraph's checkpointer itself provides).
_copilot: Optional[SRECopilot] = None


def get_copilot() -> SRECopilot:
    global _copilot
    if _copilot is None:
        # Pass the same IncidentStore the webhook/Incidents-panel code uses,
        # so get_incident_history reads from one shared, consistent view
        # instead of a second SQLite connection to the same file.
        _copilot = build_copilot(incident_store=_incidents)
    return _copilot


class ChatRequest(BaseModel):
    thread_id: str
    message: str


class ApprovalRequest(BaseModel):
    thread_id: str
    approved: bool


def _reply_json(reply: CopilotReply) -> Dict[str, Any]:
    return {
        "content": reply.content,
        "pending_action": reply.pending_action,
        "steps": reply.steps,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/api/session")
def new_session() -> Dict[str, str]:
    return {"thread_id": str(uuid.uuid4())}


@app.post("/api/chat")
def chat(req: ChatRequest) -> Dict[str, Any]:
    copilot = get_copilot()
    try:
        reply = copilot.ask(req.message, thread_id=req.thread_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return _reply_json(reply)


@app.post("/api/approve")
def approve(req: ApprovalRequest) -> Dict[str, Any]:
    copilot = get_copilot()
    try:
        reply = copilot.respond_to_approval(req.approved, thread_id=req.thread_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    # If this approval belongs to an auto-triaged incident (rather than a
    # human's own browser chat thread), reflect the outcome in the
    # Incidents panel too instead of leaving it stuck on "awaiting approval".
    record_approval_outcome(_incidents, req.thread_id, reply)
    return _reply_json(reply)


@app.post("/api/webhook/alertmanager")
def alertmanager_webhook(payload: Dict[str, Any], background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """
    Receives Alertmanager's webhook_config POST for every firing/resolved
    alert (see observability/helm/kube-prometheus-values.yaml for the
    receiver config that points Alertmanager at this endpoint). New firing
    alerts are registered immediately and investigated in the background —
    this endpoint returns right away so Alertmanager doesn't time out
    waiting on an LLM-driven investigation.
    """
    copilot = get_copilot()
    new_fingerprints = handle_alertmanager_webhook(payload, _incidents)
    for fingerprint in new_fingerprints:
        background_tasks.add_task(investigate_incident, copilot, _incidents, fingerprint)
    return {"received": len(payload.get("alerts", []) or []), "investigating": new_fingerprints}


@app.get("/api/incidents")
def incidents() -> List[Dict[str, Any]]:
    """
    All auto-triaged incidents (newest first) for the web UI's Incidents
    panel — each one the copilot picked up and investigated on its own from
    an Alertmanager webhook, with no human having asked about it first.
    """
    return [record.to_dict() for record in _incidents.list()]


@app.get("/api/alerts")
def alerts() -> List[Dict[str, Any]]:
    """
    All configured Prometheus alert rules and their current state
    (inactive/pending/firing) — backs the dashboard panel in the UI. Unlike
    the copilot's get_active_alerts tool, this includes healthy (inactive)
    rules too, so the dashboard can show the full picture, not just problems.
    """
    copilot = get_copilot()
    try:
        rules = copilot.alert_agent.get_all_alert_rules()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [
        {
            "name": rule.get("name"),
            "state": rule.get("state"),
            "severity": rule.get("labels", {}).get("severity", "unknown"),
            "service": rule.get("labels", {}).get("service", "unknown"),
            "summary": rule.get("annotations", {}).get("summary", ""),
            "description": rule.get("annotations", {}).get("description", ""),
            "active_since": (rule.get("alerts") or [{}])[0].get("activeAt"),
        }
        for rule in rules
    ]


if __name__ == "__main__":
    import uvicorn

    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    required_key = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"
    if not os.getenv(required_key):
        print(f"!! LLM_PROVIDER={provider} but {required_key} is not set; the copilot will fail to respond.")

    uvicorn.run(app, host="0.0.0.0", port=8000)
