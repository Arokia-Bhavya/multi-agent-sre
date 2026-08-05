# Multi-Agent AI SRE Platform – Project Milestones

## Project Goal
Build a production-style Multi-Agent AI SRE Platform that performs automated incident investigation and root cause analysis.

### Tech Stack
* Kubernetes
* OpenTelemetry Demo (astronomy shop app)
* Prometheus
* Grafana
* Alertmanager
* Jaeger
* OpenTelemetry
* Python
* LangGraph

---

## Milestone 1 – Observability Foundation
**Objective:** Build and validate the complete observability platform.

### ✅ Completed
- [x] Create Kind Kubernetes cluster
- [x] Deploy OpenTelemetry Demo application
- [x] Verify the OpenTelemetry Demo storefront locally at `http://localhost:8080/`
- [x] Install Prometheus
- [x] Install Grafana
- [x] Install Alertmanager
- [x] Install Jaeger
- [x] Install OpenTelemetry Collector
- [x] Configure Prometheus alert rules (`FrontendHighErrorRate`, `FrontendHighLatency`, `FrontendHighMemoryUsage`)
- [x] Fix Prometheus `ruleSelector` issue by adding `release: monitoring`
- [x] Validate the alert lifecycle (Inactive -> Pending -> Firing -> Resolved)
- [x] Inject failures using the OpenTelemetry Demo Feature Flag UI
- [x] Verify alerts fire in Alertmanager as a result of injected failures

### ⏳ Remaining
- [x] Configure OpenTelemetry Demo to export full OpenTelemetry data (traces + metrics) to the collector
- [x] Verify distributed traces in Jaeger
- [x] Verify application metrics in Prometheus
- [x] Build the first Grafana dashboard for the application

**Milestone 1 status: ✅ Complete**

---

## Milestone 2 – Observability Tool Layer
**Objective:** Build reusable Python clients for observability systems. These are reusable tools that AI agents will use.

### Target Environment
- **Application:** OpenTelemetry Demo (Astronomy Shop)
- **Local storefront:** `http://localhost:8080/`
- **Kubernetes namespace:** `otel-demo`
- **Key application services:** `frontend-proxy`, `frontend`, `checkout`, `cart`, and `product-catalog`

### ✅ Completed
- [x] Prometheus client
- [x] Jaeger client (stub)
- [x] Kubernetes client (stub)
- [x] Alertmanager client (stub)

### ⏳ Remaining
- [x] Complete and test Prometheus client with real queries
- [x] Complete and test Jaeger client
- [x] Complete and test Kubernetes client
- [x] Complete and test Alertmanager client

*Alertmanager client reads alerts directly from Prometheus's `/api/v1/alerts`
endpoint instead of a separate Alertmanager service, since the OpenTelemetry
Demo Helm deployment does not install Alertmanager by default.*

---

## Milestone 3 – Individual AI Agents
**Objective:** Develop independent agents for specific responsibilities.

### ✅ Completed
#### Metrics Agent (`ai_platform/tools/metrics_agent.py`)
* **Data Source:** Prometheus
* **Responsibilities:**
  - [x] CPU usage
  - [x] Memory usage
  - [x] Request rate
  - [x] Error rate
  - [x] Latency

#### Trace Agent (`ai_platform/tools/trace_agent.py`)
* **Data Source:** Jaeger
* **Responsibilities:**
  - [x] Retrieve traces
  - [x] Identify slow spans
  - [x] Critical path analysis

#### Kubernetes Agent (`ai_platform/tools/kubernetes_agent.py`)
* **Data Source:** Kubernetes API
* **Responsibilities:**
  - [x] Pod status
  - [x] Deployment health
  - [x] Events
  - [x] Logs
  - [x] Restarts

#### Alert Agent (`ai_platform/tools/alert_agent.py`)
* **Data Source:** Alertmanager client (backed by Prometheus `/api/v1/alerts`)
* **Responsibilities:**
  - [x] Read alerts via Alertmanager client
  - [x] Classify incidents
  - [x] Determine severity
  - [x] Route alerts (suggested channel only — real notification delivery is Milestone 6)

Unit tests: `ai_platform/tools/tests/test_agents.py` (19 tests, mocked, no cluster needed).
Smoke tests: `ai_platform/tools/test_agents_smoke.py` (needs live cluster + port-forwards).

**Milestone 3 status: ✅ Complete**

---

## Milestone 4 – LangGraph Multi-Agent Orchestration
**Objective:** Build a coordinator agent using LangGraph.

### ✅ Completed Orchestration Workflow (`ai_platform/coordinator/`)
1. [x] Receive alert — resolves affected service from alert labels or name
2. [x] Invoke Metrics Agent — runs in parallel with steps 3–4
3. [x] Invoke Trace Agent — runs in parallel with steps 2, 4
4. [x] Invoke Kubernetes Agent — runs in parallel with steps 2–3
5. [x] Correlate findings — deterministic aggregation of the three agents' output
6. [x] Produce a Root Cause Analysis (RCA) report — LLM (Claude via langchain-anthropic)
   reasons over the correlated findings and returns a structured root cause,
   confidence, evidence, and recommended actions, rendered to markdown

Unit tests: `ai_platform/coordinator/tests/test_graph.py` (11 tests, mocked agents
+ fake LLM, no cluster or API key needed).
Smoke test / CLI: `ai_platform/coordinator/run_investigation.py` (needs live
cluster + `ANTHROPIC_API_KEY`).

**Milestone 4 status: ✅ Complete**

---

## Milestone 5 – AI SRE Copilot
**Objective:** Expose the platform through a conversational interface.

### ✅ Completed (`ai_platform/copilot/`)
Built a LangGraph ReAct agent (`agent.py::SRECopilot`) over a tool layer
(`copilot_tools.py`) that wraps the Milestone 3 agents and the Milestone 4
`InvestigationGraph`, plus a CLI chat loop (`chat.py`) for interactive use.
Conversation memory is kept per `thread_id` via an in-memory LangGraph
checkpointer, so follow-up questions resolve against prior turns.

Tools exposed to the LLM: `get_service_metrics`, `get_top_cpu_consumers`,
`get_latency_by_route`, `get_slow_traces`, `get_trace_critical_path`,
`get_pod_health`, `get_deployment_health`, `get_recent_k8s_events`,
`get_pod_logs`, `get_active_alerts`, `get_critical_alerts`,
`get_incident_summary`, and `investigate_service` (runs the full Milestone 4
RCA pipeline for root-cause questions).

- [x] Answer "Why is checkout slow?" — routes to `investigate_service`
- [x] Answer "Which service has the highest CPU usage?" — `get_top_cpu_consumers`
- [x] Answer "Why did the frontend-proxy become unavailable?" — `investigate_service` / `get_pod_health`
- [x] Show the latest critical alerts — `get_active_alerts` / `get_critical_alerts`
- [x] Recommend next troubleshooting steps — `investigate_service`'s recommended actions

Unit tests: `ai_platform/copilot/tests/` (27 tests, mocked agents + a fake
tool-calling chat model, no cluster or API key needed).
CLI / smoke test: `ai_platform/copilot/chat.py` (needs live cluster,
port-forwards, and `ANTHROPIC_API_KEY` or `GROQ_API_KEY`).

**Extra, beyond the original spec: human-in-the-loop remediation.** Added one
cluster-mutating tool, `remediate_scale_deployment` (scale a deployment to a
given replica count), gated behind a real approval pause built on LangGraph's
`interrupt()`/`Command(resume=...)` — the mutating code path is unreachable
until a human approves. `SRECopilot.ask()` returns a `CopilotReply` with a
`pending_action` when a remediation is proposed; `respond_to_approval()`
continues the turn with the human's decision. See
`ai_platform/copilot/REMEDIATION_RUNBOOK.md` for an end-to-end live-cluster
test (inject fault → alert/unhealthy state → investigate → propose fix →
approve → verify repair) and `sample_remediation_demo.py` for a fully offline
walkthrough of the same flow.

**Milestone 5 status: ✅ Complete**

---

## Milestone 6 – Advanced Features
**Objective:** Enhance the platform with production-ready capabilities.

### ✅ Completed
#### Alert Correlation (`ai_platform/tools/alert_correlator.py`)
- [x] Group active alerts into correlated incidents by affected service
  (`resolve_alert_service`, mirroring `coordinator/graph.py::extract_service_name`)
- [x] Chain-cluster same-service alerts by time proximity (default 5-minute
  window) so a related burst of alerts is one incident, not several
- [x] Rank incidents by severity then alert count via `get_correlated_incidents()`
- [x] Exposed to the copilot as the `get_correlated_incidents` tool

#### Anomaly Detection (`ai_platform/tools/anomaly_detector.py`)
- [x] Z-score anomaly detection over Prometheus range queries, comparing the
  latest data point against its own recent-history baseline
- [x] Per-metric checks: `detect_cpu_anomaly`, `detect_memory_anomaly`,
  `detect_latency_anomaly`, `detect_request_rate_anomaly`
- [x] Combined `scan_service()` rollup with an overall `has_anomaly` flag
- [x] Exposed to the copilot as the `detect_service_anomalies` tool

Unit tests: `ai_platform/tools/tests/test_alert_correlator.py` (9 tests),
`ai_platform/tools/tests/test_anomaly_detector.py` (8 tests), both mocked, no
cluster needed. `copilot_tools.py` and `agent.py` updated (and their tests)
to wire both in as copilot tools.

#### Web UI (`ai_platform/webui/`)
- [x] FastAPI backend (`server.py`) wrapping the same `SRECopilot` instance
  `chat.py` uses — identical tools, conversation memory, and remediation
  approval flow, just over HTTP (`/api/session`, `/api/chat`, `/api/approve`)
  instead of stdin/stdout
- [x] Single-file browser chat UI (`static/index.html`) with per-session
  thread IDs, a live tool-call trace under each reply, and an approval
  modal that blocks Approve/Reject until the human decides — mirrors
  `chat.py`'s y/n prompt but as a proper UI gate
- [x] `CopilotReply.steps` added to `agent.py` so both the CLI's stdout
  trace and the web UI's on-screen trace come from the same structured
  data instead of the web layer scraping printed lines

Unit tests: `ai_platform/webui/tests/test_server.py` (mocked copilot, no
cluster/API key/live server needed).
Run: `uv run python ai_platform/webui/server.py`, then open `http://localhost:8000`.

#### AI-Generated Runbooks (`ai_platform/coordinator/runbook.py`)
- [x] Second structured LLM call (`RunbookDoc`) grounded in the same
  `RCAFinding` and evidence `investigate_service` already gathers —
  produces an execution-ready remediation doc (title, summary,
  prerequisites, ordered steps with concrete kubectl/PromQL commands where
  the evidence supports one, verification steps, rollback steps), kept
  separate from the shorter `RCAFinding.recommended_actions` list so
  diagnosis (RCA) and remediation (runbook) stay independent LLM calls —
  callers that only want the former don't pay for the latter
- [x] `InvestigationGraph.generate_runbook()` reruns the investigation then
  renders the runbook to markdown (`runbook_report`)
- [x] Exposed to the copilot as the `generate_runbook` tool
  (`copilot_tools.py`), which also saves the markdown to
  `ai_platform/runbooks/<service>_<alert>_<timestamp>.md` so it's a real,
  shareable document rather than just chat text
- [x] System prompt (`agent.py`) routes "runbook"/"playbook"/"how do I fix
  this" questions to `generate_runbook` instead of `investigate_service`

Unit tests: `ai_platform/coordinator/tests/test_graph.py::GenerateRunbookTests`
(4 tests) and 3 new tests in `ai_platform/copilot/tests/test_copilot_tools.py`,
all mocked, no cluster/API key/live filesystem writes outside a temp dir needed.

#### Automated Alert Auto-Triage (`ai_platform/webui/auto_responder.py`)
- [x] `POST /api/webhook/alertmanager` receives Alertmanager's webhook
  payload directly — no human has to notice an alert or ask the copilot
  about it first
- [x] New firing alerts are deduped by fingerprint, service-resolved (reuses
  `coordinator/graph.py::extract_service_name`), and investigated
  automatically in the background via `SRECopilot.ask()` on a dedicated
  per-incident thread; repeat notifications of an already-tracked firing
  alert and resolved alerts don't retrigger investigation
- [x] If the investigation surfaces a concrete remediation, it still pauses
  for human approval via the existing `interrupt()` flow — auto-triage
  automates "notice + investigate", never "change the cluster"
- [x] `GET /api/incidents` and a new Incidents panel in the web UI
  (`static/index.html`) show every auto-triaged incident (status, service,
  severity), and clicking one loads its RCA report/steps and any pending
  approval into the main chat pane
- [x] Alertmanager route/receiver wired up in
  `observability/helm/kube-prometheus-values.yaml` (webhook URL + local-kind
  connectivity notes)

Unit tests: `ai_platform/webui/tests/test_auto_responder.py` (17 tests) and
additions to `ai_platform/webui/tests/test_server.py` (4 tests), all mocked,
no cluster/API key/live server needed.

#### Incident/RCA Persistence (`ai_platform/tools/incident_store.py`)
- [x] `IncidentStore` moved out of `auto_responder.py` and given SQLite-backed
  durability (`DEFAULT_DB_PATH`, gitignored) so incident/RCA history survives
  a process restart instead of living only in an in-process dict — the
  in-memory dict remains the source of truth for reads while a process is
  running, with SQLite existing purely to reload it on the next startup.
  `IncidentRecord`/`IncidentStore` are re-exported from `auto_responder.py`
  for backward compatibility with existing imports/tests.
- [x] `IncidentStore.query_history(service, since_hours, limit)` — filters
  past incidents by service (substring match) and a time window, newest
  first
- [x] Exposed to the copilot as the `get_incident_history` tool
  (`copilot_tools.py`), wired through `SRECopilot`/`build_copilot()` in both
  `chat.py` and `server.py` (sharing one `IncidentStore` instance/db file in
  `server.py`'s case) — answers "has this happened before", "how often does
  X alert", "what was the root cause last time" questions that every other
  tool here, being live-state-only, cannot

Unit tests: `ai_platform/tools/tests/test_incident_store.py` (13 tests) and
3 new tests in `ai_platform/copilot/tests/test_copilot_tools.py`, all mocked
or against a real `:memory:`/temp-file SQLite db, no cluster/API key needed.

#### Historical Incident Search (`ai_platform/tools/incident_search.py`)
- [x] Fuzzy "have we seen something like this before" search over past
  incidents' RCA report/outcome text, for questions where the user
  describes symptoms rather than naming an exact service — complements
  `get_incident_history`'s exact service/time filters
- [x] Implemented as TF-IDF + cosine similarity (scikit-learn), not learned
  embeddings: neither Anthropic nor Groq (this project's two LLM providers)
  expose an embeddings API, and a local embedding model (e.g.
  sentence-transformers) would pull in a heavy torch dependency for what's
  meant to stay a lightweight demo platform. This is keyword/phrase-overlap
  similarity, not true semantic similarity — it won't catch a paraphrase
  with zero shared vocabulary — documented as a deliberate tradeoff rather
  than presented as full semantic RAG
  - `search_similar_incidents(records, query, top_k, min_similarity)`
    rebuilds the TF-IDF index fresh from current incidents on every call
    (correct and simple at the incident volumes this platform expects)
- [x] Exposed to the copilot as the `search_similar_incidents` tool
  (`copilot_tools.py`); system prompt routes symptom-described questions
  here and service/time-specific ones to `get_incident_history`

Unit tests: `ai_platform/tools/tests/test_incident_search.py` (8 tests) and
2 new tests in `ai_platform/copilot/tests/test_copilot_tools.py`, pure logic,
no cluster/API key needed.

### 🔧 Hardening pass (post-Milestone 6)

A review of the whole codebase produced the following. Two confirmed runtime
bugs were fixed; the security findings are recorded here as the input to the
planned guardrails work rather than being fixed yet.

#### Fixed: alert → service misattribution (`ai_platform/tools/service_matching.py`)
- [x] The old matcher squashed an alert name to lowercase-alphanumeric and
  asked whether a known service appeared anywhere inside it. The two-letter
  service `ad` therefore matched the "ad" in re**ad**y, lo**ad**, and
  relo**ad**, so stock kube-prometheus-stack alerts resolved to the wrong
  service: `KubeNodeNotReady` → `ad`, `HighLoadAverage` → `ad`,
  `AlertmanagerFailedReload` → `ad`. With the Milestone 6 webhook wired up
  these arrive automatically, so each one spent a full multi-LLM-call
  investigation on an unrelated service and filed a confidently wrong RCA.
- [x] Replaced with token matching on word boundaries: names split on
  CamelCase/hyphen/underscore, and a service matches only if its tokens form
  a contiguous run (or its squashed form is exactly one token, covering
  `LoadgeneratorDown` → `load-generator`). Most-specific-first ranking keeps
  `frontend-proxy` winning over `frontend`.
- [x] Collapsed the duplication this exposed: `coordinator/graph.py::extract_service_name`
  and `tools/alert_correlator.py::resolve_alert_service` each carried their
  own copy of the matcher *and* of `DEFAULT_KNOWN_SERVICES`, with a comment
  acknowledging the drift risk. Both are now thin aliases over one shared
  implementation, and the service list is defined once.
- [x] Verified against the real stock alert set: 0/42 infrastructure alerts
  misattributed (was 3+), 13/13 application alerts still resolve correctly,
  no service unreachable, explicit labels still authoritative.
- Note: infrastructure alerts now resolve to `None` instead of a wrong
  service. `_receive_alert` already handles that — it records a `data_gap`
  and the agent nodes short-circuit — so the output is "could not determine
  affected service" rather than a fabricated RCA. Whether the webhook should
  filter non-application alerts *before* spending an investigation on them is
  still open.

Unit tests: `ai_platform/tools/tests/test_service_matching.py` (23 tests).

#### Fixed: shared mutable default on `CopilotReply.steps` (`copilot/agent.py`)
- [x] `steps` was a `NamedTuple` field defaulting to a literal `[]`, which
  Python evaluates once at class-definition time — so every reply built
  without an explicit `steps=` shared one list object, and mutating any of
  them would corrupt all the others plus every reply created afterwards.
  `auto_responder.py` persists `reply.steps` straight to SQLite, so a leaked
  mutation would have been written to disk and shown against unrelated
  incidents in the Incidents panel.
- [x] Converted to a frozen dataclass with `field(default_factory=list)`.
  Nothing depended on tuple behaviour (no unpacking, no index access), so the
  change is transparent to callers — and freezing means a stray mutation now
  fails loudly instead of silently.

Unit tests: `CopilotReplyDefaultsTests` in `ai_platform/copilot/tests/test_agent.py` (4 tests).

#### Fixed: the test suite could not actually be run
- [x] `pytest` was never declared as a dependency, and with it installed,
  bare `pytest` from the repo root failed to collect **all 13 test modules**
  with `ModuleNotFoundError` — the flat imports (`from metrics_agent import
  MetricsAgent`) depend on four directories being on `sys.path`, which only
  the entry points and two `webui` test files set up.
- [x] Added a `dev` dependency group (`pytest`, `httpx`) and
  `[tool.pytest.ini_options]` with `pythonpath` (the four source dirs) and
  `testpaths` (the four `tests/` dirs). `testpaths` also keeps
  `tools/test_clients.py`, `test_agents_smoke.py`, and
  `test_milestone6_smoke.py` out of the offline run — they're live-cluster
  smoke scripts whose `test_*.py` names made pytest collect them.
- [x] `uv run pytest` → **180 passed**.

#### Fixed: repository cleanup
- [x] Removed all Google Online Boutique leftovers — the project migrated to
  the OpenTelemetry Demo but the `boutique/` dir, a Grafana dashboard whose
  every panel queried the dead `boutique` namespace, an alert rule targeting
  two namespaces that no longer exist, and ~15 README references all
  survived. Also removed two Helm values files installing into the retired
  `observability` namespace, three zero-byte alert files, and a duplicate
  `otel-demo-values.yaml` whose alerts were a strict subset of
  `otel-demo-minimal-values.yaml`.
- [x] Pinned Python 3.14 consistently (`.python-version` said 3.14,
  `pyproject.toml` said `>=3.11`, README said 3.11+).

#### ⚠️ Open security findings (input to the guardrails work)
- [x] **The human approval gate is bypassable over the network.**
  `server.py` binds `0.0.0.0` and no endpoint authenticates, so
  `POST /api/approve` from anyone who can reach the port executes a pending
  `scale_deployment`. Thread IDs aren't secret either — `GET /api/incidents`
  returns every one of them unauthenticated.
  Fixed: default bind changed to `127.0.0.1` (`WEBUI_HOST` to override), and
  every `/api/*` route now requires a shared-secret `X-API-Key`/Bearer
  header — auto-generated and printed at startup if `WEBUI_API_KEY` isn't
  set (`server._resolve_api_key`), rather than left open by default.
  Explicit `WEBUI_API_KEY=` (empty) is the only way back to fully open.
  Still just a shared secret, not real per-user auth — see ARCHITECTURE.md.
- [x] **Stored XSS chaining into the above.** `static/index.html` interpolates
  `action.reason`, `rule.name`/`rule.summary`, and
  `record.alert_name`/`record.service` into `innerHTML` unescaped. Since the
  webhook is unauthenticated, an attacker-supplied `alertname` reaches the
  Incidents panel as executable script, which can then POST its own approval.
  Fixed: `showApproval`, `renderAlerts`, `renderIncidents` now build DOM
  nodes with `textContent` instead of interpolating into `innerHTML`.
- [x] **PromQL injection.** `metrics_agent.py` f-strings `service_name`
  directly into queries, and that value arrives from LLM tool calls fed by
  untrusted alert annotations. Read-only, so the ceiling is wrong data — but
  wrong data drives the RCA.
  Fixed: `prometheus_http_client.escape_label_value` (PromQL string-literal
  escaping) applied everywhere a service/container name is interpolated
  into a label matcher, in both `metrics_agent.py` and `anomaly_detector.py`.
- [x] **Unbounded background investigations.** Every new fingerprint spawns a
  `BackgroundTasks` investigation with no semaphore, queue, or budget cap. A
  40-alert cascade — the normal shape of a real outage — fires 40 concurrent
  multi-LLM-call investigations.
  Fixed: `auto_responder.py` now gates `investigate_incident` behind a
  bounded `threading.Semaphore` (`MAX_CONCURRENT_INVESTIGATIONS`, default
  3); incidents past the cap show status `queued` instead of piling on
  concurrent LLM/tool pipelines.
- [x] **Import architecture.** 20 `sys.path.insert` calls, inconsistent
  `__init__.py` placement, and a package dir (`ai-platform`) whose hyphen
  makes it un-importable. Module import success is order-dependent. Also,
  `tools/prometheus_client.py` shadows the official PyPI `prometheus_client`.
  Fully addressed, in two passes:
  - First pass (contained/mechanical): renamed `prometheus_client.py` to
    `prometheus_http_client.py`; replaced the ~20 scattered
    `sys.path.insert` blocks with one shared `_bootstrap.py::setup_paths()`;
    added `__init__.py` to `tools/tests/`/`coordinator/tests/` for
    consistency with `copilot/tests/`/`webui/tests/`; removed three empty
    leftover dirs (`agents/`, `api/`, `prompts/`).
  - Second pass (the full rewrite the first pass deliberately deferred):
    `ai-platform` → `ai_platform` (a valid Python identifier), with a root
    `ai_platform/__init__.py`. Every internal import converted from a flat
    bare name (`from metrics_agent import MetricsAgent`) to a real
    package-qualified one (`from ai_platform.tools.metrics_agent import
    MetricsAgent`) — 37 files, done with a small scripted rewrite plus
    manual fixes for `unittest.mock.patch("module.attr", ...)` string
    targets, which the scripted pass couldn't see. `_bootstrap.py` deleted
    entirely along with every remaining `sys.path.insert` call.
    `pyproject.toml` gained a real `[build-system]` (hatchling) +
    `[tool.hatch.build.targets.wheel] packages = ["ai_platform"]` +
    `[tool.uv] package = true`, so `uv sync` installs the project itself in
    editable mode; `[tool.pytest.ini_options]` lost its `pythonpath` hack
    (pytest now resolves `ai_platform.*` imports itself via the complete
    `__init__.py` chain up to the repo root). Entry points now run as
    `python -m ai_platform.<pkg>.<module>` rather than
    `python ai_platform/<pkg>/<module>.py`, since plain script execution
    doesn't put the repo root on `sys.path` the way `-m` does.
    Verified via the full test suite (passing with zero `pythonpath`
    overrides) plus direct import/`-m` smoke tests of all five entry points.

---

### 🔒 Production-grade web UI auth + Incident Timeline (post-Milestone 6)

#### Google OIDC auth (`ai_platform/webui/server.py`)
- [x] Replaced the shared-secret `WEBUI_API_KEY` model for every
  human-facing route with real per-user authentication: Google OIDC
  (`/auth/login` → Google → `/auth/callback`), backed by Authlib and a
  signed, httponly session cookie (`SessionMiddleware`, `SESSION_SECRET_KEY`)
- [x] `/login` page with a "Sign in with Google" button (`static/login.html`);
  unauthenticated requests to `/` redirect there, unauthenticated `/api/*`
  requests get a 401 instead of a silent shared-secret prompt
- [x] Optional `ALLOWED_GOOGLE_EMAILS`/`ALLOWED_GOOGLE_DOMAIN` allowlists —
  authorization layered on top of Google's authentication, since a bare
  Google sign-in only proves identity, not that this specific person should
  have access
- [x] `WEBUI_API_KEY` narrowed to exactly one caller instead of every route:
  Alertmanager's webhook POST, which can't complete an OAuth redirect since
  it isn't a browser — everything else now goes through the session above
- [x] `GET /api/me` + a user badge/sign-out link in the web UI header
  (`static/index.html`)
- See ARCHITECTURE.md's "Known gaps" for what's still open (no token
  refresh, no per-role authorization within a session, secrets still in
  plaintext `.env`)

#### Dedicated Incident Timeline (`ai_platform/webui/static/index.html`)
- [x] New "Timeline" tab alongside the existing chat view, built on the same
  `GET /api/incidents` data as the sidebar's Incidents panel (no new backend
  endpoint needed — `IncidentRecord.created_at`/`updated_at` already existed)
- [x] Incidents grouped by calendar day, ordered oldest-to-newest within a
  day, each rendered as a dated timeline entry with status/severity/service
  badges; clicking one jumps to its RCA/approval in the Chat tab (reuses
  `openIncident`)
- [x] Built with `textContent`, not `innerHTML`, consistent with the
  existing XSS fix — alert names/services in timeline entries still trace
  back to an Alertmanager payload

#### PII/secret redaction (`ai_platform/tools/pii_redaction.py`)
- [x] The copilot's read tools return real cluster data — pod logs,
  Kubernetes event messages, RCA reports — straight to a third-party LLM
  API (Anthropic/Groq), and that data then gets persisted indefinitely in
  `IncidentStore`'s SQLite file. Nothing upstream guaranteed those logs
  never contained a customer email, phone number, SSN, card number, or a
  stray secret a service happened to log.
- [x] Added a best-effort regex scrub (`redact_text`/`redact_structure`)
  applied at two points: the source
  (`KubernetesClient.get_pod_logs`/`get_recent_events`) and again as a
  defense-in-depth backstop over every read tool's output
  (`copilot_tools._serialize`), so a single missed call site doesn't mean
  unredacted data ships. IPs/hostnames are deliberately left alone — core
  operational signal, not personal data on their own.
  Pattern-based, so it's a floor, not a compliance-grade guarantee; see
  ARCHITECTURE.md's "Known gaps."
- Unit tests: `ai_platform/tools/tests/test_pii_redaction.py`.

#### Operations Hub tab + sidebar removal (`ai_platform/webui/`)
- [x] New `GET /api/operations-hub?range_days=7|14|30` endpoint aggregates
  `IncidentStore` records into KPIs — total incidents, resolution rate,
  median time to mitigate (from resolved incidents'
  `updated_at - created_at`), pending-approval count, a per-day
  resolved/investigating/awaiting-approval/error breakdown, and the 5 most
  recent incidents. No new data source — pure aggregation of what the
  Incidents panel and Timeline already read.
- [x] New "Operations hub" tab in `static/index.html`, reusing the existing
  dark-theme tokens and `.incident-card`/`.badge` classes: 4 KPI cards, a
  stacked daily bar chart, and a recent-incidents list that jumps into
  Chat on click (same `openIncident` path as Timeline). Set as the default
  landing tab.
- [x] Removed the persistent left sidebar (live Alerts + an
  Auto-Investigated-Incidents list) — redundant once Timeline and
  Operations Hub covered the same incident data in the main pane. The
  `GET /api/alerts` endpoint itself is unchanged and still available.
- Unit tests: `OperationsHubTests` in `ai_platform/webui/tests/test_server.py`
  (9 tests) — auth, empty-state, resolution-rate/median-duration math,
  date-range filtering, recent-list ordering/cap.

#### Conversation history trimming (`ai_platform/copilot/agent.py`)
- [x] `SRECopilot` used `InMemorySaver()` with no message trimming or
  summarization — every turn in a thread resent the *entire* accumulated
  history (every question, every tool call, every raw tool result,
  including a full `investigate_service` RCA report) to the LLM. Token
  cost per turn grew with conversation length instead of staying roughly
  flat, and a chat session or auto-triaged incident with a few
  investigations could burn through a provider's per-minute/per-day token
  cap (encountered in practice on Groq's free tier) far faster than the
  underlying questions actually required.
- [x] Added `_trim_history`, a `pre_model_hook` passed to
  `create_react_agent`, that caps what's sent to the LLM each turn to the
  most recent `MAX_HISTORY_TOKENS` (6000, leaving headroom under Groq
  free-tier llama-3.3-70b-versatile's 12,000 TPM cap after the system
  prompt and a fresh tool round-trip). Returns
  `{"llm_input_messages": ...}` rather than `{"messages": ...}` — trims
  only what the model sees, not what's persisted in the checkpointer, so
  the web UI's tool-call trace and `chat.py`'s transcript still show full
  history even once older turns have aged out of the model's own context.
  `start_on`/`end_on="human"` keep the trimmed window on valid boundaries
  (Anthropic/Groq both reject a message list opening with a dangling tool
  result or ending mid-tool-call).
- Unit tests: `TrimHistoryTests` in `ai_platform/copilot/tests/test_agent.py`
  (4 tests) — short history passes through unchanged, long history is
  trimmed, the most recent turn is preserved, and the trimmed window
  always starts on a human message.

### 🧠 Knowledge base + semantic search (post-Milestone 6)
- [x] `ai_platform/tools/embeddings_client.py` — plain `requests` REST
  client for Voyage AI (Anthropic's recommended embeddings partner, free
  tier, no extra SDK needed for one endpoint). `is_configured()`/
  `embed_texts()`; every call site treats it as optional, degrading to
  existing TF-IDF behavior when `VOYAGE_API_KEY` isn't set.
- [x] `ai_platform/tools/semantic_search.py` — shared `rank_by_similarity`
  used by both search tools below: tries Voyage embeddings first (true
  semantic similarity, catches a paraphrase with zero shared vocabulary),
  falls back to the original TF-IDF + cosine similarity logic (moved here
  from `incident_search.py`) when unconfigured or on any API failure.
- [x] `search_similar_incidents` (`incident_search.py`) refactored onto
  `rank_by_similarity` — same public signature and behavior for existing
  callers/tests, now embeddings-backed when a key is configured instead of
  TF-IDF-only.
- [x] `ai_platform/tools/knowledge_base.py` — new "knowledge base
  integration" tool, distinct from incident history search: searches
  human-authored playbooks under `knowledge_base/` at the repo root (one
  `*.md` file per failure pattern, whole-file granularity) rather than
  this platform's own auto-generated RCA history, using the same
  embeddings-first/TF-IDF-fallback ranking. Seeded with four starter
  playbooks (`high-error-rate.md`, `high-latency.md`, `pod-down.md`,
  `high-memory-usage.md`) matching the alert rules in
  `observability/helm/otel-demo-minimal-values.yaml`.
- [x] Exposed to the copilot as the `search_knowledge_base` tool
  (`copilot_tools.py`), added to `CORE_COPILOT_TOOL_NAMES`; system prompt
  updated to route "is there a documented playbook for X" questions here
  rather than to `search_similar_incidents`.
- [x] `.env.example` documents the optional `VOYAGE_API_KEY`/
  `VOYAGE_EMBED_MODEL` vars; `numpy` added explicitly to `pyproject.toml`
  (was already a transitive scikit-learn dependency, now imported
  directly by `semantic_search.py`).

Unit tests: `ai_platform/tools/tests/test_embeddings_client.py`,
`test_semantic_search.py`, `test_knowledge_base.py` — all mocked, no
network/API key needed. Existing `test_incident_search.py` (8 tests)
passes unchanged (no `VOYAGE_API_KEY` in the test environment, so it still
exercises the TF-IDF path).

---
*Note: After Milestone 1 is complete, development shifts primarily to Python and LangGraph for building the AI-powered incident investigation platform.*
