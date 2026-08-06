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
    - fastapi, uvicorn, authlib, itsdangerous (pip/uv install)

Run (from the repo root):
    uv run python -m ai_platform.webui.server

Then open http://localhost:8000 — you'll be redirected to /login to sign in
with Google before anything else loads.
"""

import hmac
import os
import secrets
import statistics
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from authlib.integrations.starlette_client import OAuth
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware


# --- Human auth: Google OIDC ------------------------------------------------
#
# README.md's "Known Limitations" flagged the original shared-secret
# WEBUI_API_KEY model as "a single static key with no rotation or per-user
# scoping ... not proper auth", and named OIDC in front of the server as the
# fix. This replaces it for every human-facing route: signing in exchanges a
# Google account for a signed, httponly session cookie — there's a real
# per-user identity (`request.session["user"]["email"]`) instead of one
# secret everyone shares.
#
# The one caller that keeps the old shared-secret model is Alertmanager's own
# webhook POST (`/api/webhook/alertmanager`) — it's a machine, not a browser,
# so it can't complete an OAuth redirect. See `_require_auth` below.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
OIDC_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# Optional allowlist: without one, *any* Google account can sign in, which is
# real authentication (a verified identity) but not yet authorization. Most
# deployments of this platform will want at least one of these set.
_allowed_emails_raw = os.environ.get("ALLOWED_GOOGLE_EMAILS", "")
ALLOWED_GOOGLE_EMAILS = {e.strip().lower() for e in _allowed_emails_raw.split(",") if e.strip()}
ALLOWED_GOOGLE_DOMAIN = os.environ.get("ALLOWED_GOOGLE_DOMAIN", "").strip().lower()

oauth = OAuth()
if OIDC_CONFIGURED:
    oauth.register(
        name="google",
        server_metadata_url=GOOGLE_DISCOVERY_URL,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"},
    )


def _user_allowed(userinfo: Dict[str, Any]) -> bool:
    """
    Authorization on top of authentication: a verified Google identity is
    required either way, but if an allowlist is configured, only emails (or
    a domain) on it may actually get a session.
    """
    if userinfo.get("email_verified") is False:
        return False
    email = (userinfo.get("email") or "").lower()
    if not email:
        return False
    if ALLOWED_GOOGLE_EMAILS and email not in ALLOWED_GOOGLE_EMAILS:
        return False
    if ALLOWED_GOOGLE_DOMAIN and not email.endswith("@" + ALLOWED_GOOGLE_DOMAIN):
        return False
    return True


# Signs the session cookie (an httponly, SameSite=Lax cookie holding the
# user's email/name/picture — never a secret the user could replay
# elsewhere). Read once at import time, same pattern as WEBUI_API_KEY below,
# so an operator can pin SESSION_SECRET_KEY in .env for sessions that survive
# a process restart, or leave it unset for a fresh one each time (existing
# sessions are simply invalidated, not a security hole).
def _resolve_session_secret() -> tuple:
    raw = os.environ.get("SESSION_SECRET_KEY")
    if raw:
        return raw, False
    return secrets.token_urlsafe(32), True


SESSION_SECRET_KEY, SESSION_SECRET_AUTO_GENERATED = _resolve_session_secret()

# Cookies only get the `Secure` flag when explicitly told the deployment is
# behind HTTPS (e.g. via a reverse proxy) — forcing it on by default would
# silently break local http://localhost:8000 development.
COOKIE_SECURE = os.environ.get("WEBUI_COOKIE_SECURE", "false").lower() == "true"


# --- Machine auth: shared secret, Alertmanager webhook only -----------------
#
# Previously gated *every* /api/* route (see git history / README.md).
# Now scoped to just the one machine-to-machine caller that can't do an OIDC
# browser redirect: Alertmanager's webhook_configs POST. Kept for exactly the
# reason the original comment gave — Alertmanager supports a Bearer token via
# `http_config.authorization` natively, but not an OAuth flow.
def _resolve_api_key() -> tuple:
    raw = os.environ.get("WEBUI_API_KEY")
    if raw is None:
        return secrets.token_urlsafe(32), True
    return raw, False


WEBUI_API_KEY, API_KEY_AUTO_GENERATED = _resolve_api_key()

from ai_platform.copilot.chat import build_copilot  # reuses the exact same agent/tool wiring as the CLI
from ai_platform.copilot.agent import SRECopilot, CopilotReply
from ai_platform.webui.auto_responder import (
    DEFAULT_DB_PATH,
    IncidentStore,
    handle_alertmanager_webhook,
    investigate_incident,
    record_approval_outcome,
)
from ai_platform.tools.env_hygiene import check_env_file_permissions


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="AI SRE Copilot Web UI")


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    """
    Two distinct auth models, one for machines and one for humans:

    - `/api/webhook/alertmanager` keeps the shared-secret model (Bearer
      token or X-API-Key) — Alertmanager cannot complete a browser OAuth
      redirect, so this is the one deliberate exception.
    - Every other `/api/*` route requires a signed-in Google session
      (`request.session["user"]`), set by `/auth/callback` below.
    - `/`, the SPA shell, redirects to `/login` if there's no session yet
      (only once OIDC is actually configured — see note below).
    - `/login`, `/auth/*`, and `/static/*` always stay open: they're either
      the sign-in surface itself or static assets with no cluster data.

    Registered *before* `SessionMiddleware` is added further down, so that
    `SessionMiddleware` ends up outermost in the stack (Starlette wraps
    later-added middleware around earlier-added ones) and has already
    populated `request.session` by the time this function runs.
    """
    path = request.url.path

    if path == "/api/webhook/alertmanager":
        if WEBUI_API_KEY:
            provided = request.headers.get("x-api-key", "")
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided = provided or auth_header[len("Bearer "):]
            # Constant-time comparison: a naive `!=` leaks how many leading
            # characters matched via response-timing.
            if not hmac.compare_digest(provided, WEBUI_API_KEY):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid X-API-Key/Authorization header."},
                )
        return await call_next(request)

    if path.startswith("/api"):
        if not request.session.get("user"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated. Sign in at /login."},
            )
        return await call_next(request)

    if path == "/" and OIDC_CONFIGURED and not request.session.get("user"):
        return RedirectResponse(url="/login")

    return await call_next(request)


# SessionMiddleware added after `_require_auth` above (see its docstring for
# why the order matters) so it's the outermost middleware and has already
# attached `request.session` before `_require_auth` reads it.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=8 * 60 * 60,  # 8-hour session
)

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


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    """
    Unauthenticated liveness/readiness probe for container orchestrators
    (Docker healthcheck, Kubernetes livenessProbe/readinessProbe). Doesn't
    hit the database or any live cluster connection — those are checked
    lazily on first real use (get_copilot(), IncidentStore) rather than
    here, so this stays cheap enough to poll frequently and doesn't report
    "unhealthy" just because Prometheus/Jaeger/K8s aren't reachable yet at
    startup.

    Falls through `_require_auth` untouched (path doesn't match "/api" or
    "/") — deliberately so; an orchestrator's health probe has no session
    or API key to present.
    """
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/auth/login")
async def auth_login(request: Request):
    if not OIDC_CONFIGURED:
        raise HTTPException(
            status_code=503,
            detail="Google OIDC is not configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not OIDC_CONFIGURED:
        raise HTTPException(status_code=503, detail="Google OIDC is not configured.")
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if userinfo is None:
        userinfo = await oauth.google.userinfo(token=token)
    if not _user_allowed(userinfo):
        return RedirectResponse(url="/login?error=not_allowed")
    request.session["user"] = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("email"),
        "picture": userinfo.get("picture"),
    }
    return RedirectResponse(url="/")


@app.get("/auth/logout")
def auth_logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login")


@app.get("/api/me")
def me(request: Request) -> Dict[str, Any]:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


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
    alert (see observability/helm/otel-demo-minimal-values.yaml for the
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
    panel and Timeline view — each one the copilot picked up and
    investigated on its own from an Alertmanager webhook, with no human
    having asked about it first. `created_at`/`updated_at` on every record
    are what the Timeline view plots chronologically.
    """
    return [record.to_dict() for record in _incidents.list()]


_RESOLVED_STATUSES = {"completed", "resolved"}


def _parse_iso(value: str) -> datetime:
    """
    Best-effort ISO-8601 parse, tolerant of naive timestamps (treated as
    UTC) — same approach as IncidentStore.query_history's local helper, so
    the two stay consistent about how `created_at`/`updated_at` are read.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@app.get("/api/operations-hub")
def operations_hub(range_days: int = 7) -> Dict[str, Any]:
    """
    Aggregate incident-analytics KPIs for the web UI's Operations Hub tab —
    everything here is derived from `IncidentStore` records already
    collected for the Incidents panel/Timeline, no new data source.

    `range_days` (default 7) bounds both the KPI totals and the daily
    volume/outcomes breakdown to incidents created in that window.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=range_days)

    records = [r for r in _incidents.list() if _parse_iso(r.created_at) >= cutoff]

    total = len(records)
    resolved = [r for r in records if r.status in _RESOLVED_STATUSES]
    pending_approvals = sum(1 for r in records if r.status == "awaiting_approval")

    resolution_rate = (len(resolved) / total) if total else 0.0

    mitigate_seconds = []
    for r in resolved:
        created = _parse_iso(r.created_at)
        updated = _parse_iso(r.updated_at)
        delta = (updated - created).total_seconds()
        if delta >= 0:
            mitigate_seconds.append(delta)
    median_time_to_mitigate_seconds = statistics.median(mitigate_seconds) if mitigate_seconds else None

    # Daily bucket, oldest to newest, one entry per calendar day in range
    # (even days with zero incidents) so the chart doesn't skip gaps.
    daily: "OrderedDict[str, Dict[str, int]]" = OrderedDict()
    for offset in range(range_days - 1, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        daily[day] = {"resolved": 0, "investigating": 0, "awaiting_approval": 0, "error": 0}
    for r in records:
        day = _parse_iso(r.created_at).strftime("%Y-%m-%d")
        bucket = daily.get(day)
        if bucket is None:
            continue
        if r.status in _RESOLVED_STATUSES:
            bucket["resolved"] += 1
        elif r.status == "error":
            bucket["error"] += 1
        elif r.status == "awaiting_approval":
            bucket["awaiting_approval"] += 1
        else:
            bucket["investigating"] += 1

    recent = [r.to_dict() for r in records[:5]]

    return {
        "range_days": range_days,
        "total_incidents": total,
        "resolved_count": len(resolved),
        "resolution_rate": resolution_rate,
        "pending_approvals": pending_approvals,
        "median_time_to_mitigate_seconds": median_time_to_mitigate_seconds,
        "daily": [{"date": day, **counts} for day, counts in daily.items()],
        "recent": recent,
    }


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

    env_warning = check_env_file_permissions()
    if env_warning:
        print(env_warning)

    if not OIDC_CONFIGURED:
        print("=" * 72)
        print("!! GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not set — /login and")
        print("   /auth/login will 503, and every /api/* route (other than the")
        print("   Alertmanager webhook) is unreachable from the browser until you")
        print("   configure Google OIDC. See .env.example for how to create a")
        print("   Google OAuth client and which redirect URI to register.")
        print("=" * 72)
    elif not ALLOWED_GOOGLE_EMAILS and not ALLOWED_GOOGLE_DOMAIN:
        print(
            "!! No ALLOWED_GOOGLE_EMAILS or ALLOWED_GOOGLE_DOMAIN configured — "
            "any Google account can sign in. Set one of them to restrict access."
        )

    if SESSION_SECRET_AUTO_GENERATED:
        print(
            "No SESSION_SECRET_KEY set — using a random one for this process. "
            "Existing sessions won't survive a restart; set SESSION_SECRET_KEY "
            "in .env for sessions that do."
        )

    if API_KEY_AUTO_GENERATED:
        print("=" * 72)
        print("No WEBUI_API_KEY set — generated a one-time key for this session,")
        print("used only to authenticate Alertmanager's webhook POST (human")
        print("access now goes through Google sign-in instead):")
        print(f"    {WEBUI_API_KEY}")
        print("Set WEBUI_API_KEY in .env for a stable key that survives")
        print("restarts, or `WEBUI_API_KEY=` (empty) to disable webhook auth")
        print("entirely — not recommended beyond loopback-only use.")
        print("=" * 72)
    elif not WEBUI_API_KEY:
        print("!! WEBUI_API_KEY is explicitly empty — the Alertmanager webhook endpoint is unauthenticated.")

    # Defaults to localhost, not 0.0.0.0: even with real auth in front of it
    # now, there's no reason to widen the network surface unless you mean to.
    # Set WEBUI_HOST=0.0.0.0 explicitly (and WEBUI_COOKIE_SECURE=true behind
    # a TLS-terminating proxy) if you deliberately want this reachable beyond
    # this machine.
    host = os.getenv("WEBUI_HOST", "127.0.0.1")
    if host != "127.0.0.1" and not OIDC_CONFIGURED:
        print(f"!! WEBUI_HOST={host} with Google OIDC unconfigured — the UI will be unreachable for anyone but the Alertmanager webhook.")

    uvicorn.run(app, host=host, port=8000)
