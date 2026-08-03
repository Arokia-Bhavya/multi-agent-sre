# Multi-Agent AI SRE Platform — Architecture

Layered view of the platform, from the two front ends down to the Kind
cluster. Solid arrows are read paths. The dashed orange path is the
platform's only cluster-mutating action, gated behind human approval.

There are two ways in. **Pull** — a human asks the copilot a question via the
CLI or the browser. **Push** — Alertmanager posts a firing alert to the
webhook and the copilot investigates it with nobody watching. Both converge
on the same `SRECopilot` instance, the same 19 tools, and the same approval
gate.

```mermaid
flowchart TD
    You(["You<br/><small>chat.py REPL · web UI</small>"])
    AM_Push(["Alertmanager<br/><small>webhook: firing alert</small>"])

    subgraph Frontend_Layer["Web UI (FastAPI)"]
        Server["server.py<br/><small>/api/chat · /api/approve · /api/incidents</small>"]
        AutoResponder["auto_responder.py<br/><small>dedup by fingerprint, background triage</small>"]
        Store[("IncidentStore<br/><small>SQLite: RCA history</small>")]
    end

    subgraph Copilot_Layer["AI SRE Copilot"]
        Copilot["SRE Copilot<br/><small>LangGraph ReAct agent · 19 tools · approval gate</small>"]
    end

    subgraph Coordinator_Layer["Coordinator"]
        Graph["Investigation Graph<br/><small>correlate + LLM root cause + runbook</small>"]
    end

    subgraph Analysis_Layer["Analysis (Milestone 6)"]
        Correlator["Alert Correlator<br/><small>group by service + time</small>"]
        Anomaly["Anomaly Detector<br/><small>z-score vs. own history</small>"]
        Search["Incident Search<br/><small>TF-IDF over past RCAs</small>"]
    end

    subgraph Agents_Layer["Agents"]
        MetricsAgent["Metrics Agent<br/><small>rate / errors / latency</small>"]
        TraceAgent["Trace Agent<br/><small>slow spans, critical path</small>"]
        K8sAgent["Kubernetes Agent<br/><small>pods, deploys, logs</small>"]
        AlertAgent["Alert Agent<br/><small>classify, route</small>"]
    end

    subgraph Clients_Layer["Clients"]
        PromClient["Prometheus client<br/><small>read</small>"]
        JaegerClient["Jaeger client<br/><small>read</small>"]
        K8sClient["Kubernetes API client<br/><small>read + 1 write</small>"]
        AMClient["Alertmanager client<br/><small>via Prometheus API</small>"]
    end

    subgraph Cluster_Layer["Kind cluster"]
        OtelDemo["OTel Demo App<br/><small>ns: otel-demo</small>"]
        Prometheus["Prometheus"]
        Jaeger["Jaeger"]
        Grafana["Grafana / Alertmanager<br/><small>ns: monitoring</small>"]
    end

    You --> Server
    AM_Push --> AutoResponder
    Server --> Copilot
    AutoResponder --> Copilot
    AutoResponder <--> Store
    Server <--> Store
    Copilot -- "get_incident_history<br/>search_similar_incidents" --> Store

    Copilot -- "investigate_service<br/>generate_runbook" --> Graph
    Copilot -- "16 direct read tools" --> MetricsAgent
    Copilot -.-> TraceAgent
    Copilot -.-> K8sAgent
    Copilot -.-> AlertAgent
    Copilot -.-> Correlator
    Copilot -.-> Anomaly
    Copilot -.-> Search

    Graph -- "parallel invoke" --> MetricsAgent
    Graph -.-> TraceAgent
    Graph -.-> K8sAgent

    Correlator --> AlertAgent
    Anomaly --> PromClient
    Search --> Store

    MetricsAgent --> PromClient
    TraceAgent --> JaegerClient
    K8sAgent --> K8sClient
    AlertAgent --> AMClient

    PromClient --> Prometheus
    JaegerClient --> Jaeger
    K8sClient --> OtelDemo
    AMClient --> Prometheus

    Prometheus -. "alert rules" .- Grafana
    Grafana -. "webhook" .-> AM_Push

    Copilot == "① propose fix" ==> Approval{{"② interrupt()<br/>human approves?"}}
    Approval == "③ scale_deployment (write)" ==> K8sClient

    classDef write stroke:#e07b39,stroke-width:2px,color:#e07b39;
    class Approval write;
```

## Components

- **Clients** (`ai-platform/tools/*_client.py`, `alertmanager.py`): thin,
  mostly read-only wrappers around Prometheus, Jaeger, Kubernetes, and
  Alertmanager (backed by Prometheus's `/api/v1/alerts`, since the demo chart
  doesn't ship Alertmanager).
- **Agents** (`ai-platform/tools/*_agent.py`): domain logic over the
  clients — Metrics, Trace, Kubernetes, and Alert agents.
- **Analysis** (`ai-platform/tools/`): `alert_correlator.py` groups related
  alerts into incidents, `anomaly_detector.py` flags metrics behaving
  unusually for their own history, `incident_search.py` does TF-IDF lookup
  over past RCA text.
- **Service resolution** (`ai-platform/tools/service_matching.py`): maps an
  alert to the service it's about, by label first and token-matched alert
  name second. Shared by the coordinator and the correlator so the two can't
  drift apart.
- **Investigation Graph** (`ai-platform/coordinator/`): a LangGraph
  coordinator that gathers metrics/traces/Kubernetes evidence in parallel,
  correlates it, and has an LLM produce a structured root-cause report
  (`rca.py`) and, on request, a separate execution-ready runbook
  (`runbook.py`).
- **SRE Copilot** (`ai-platform/copilot/`): a conversational LangGraph ReAct
  agent over all of the above. Includes one cluster-mutating tool,
  `remediate_scale_deployment`, gated by a real approval pause
  (`interrupt()` / `Command(resume=...)`) — the write path is unreachable
  until a human approves.
- **Web UI** (`ai-platform/webui/`): FastAPI backend wrapping the same
  copilot instance the CLI uses, plus `auto_responder.py`, which turns the
  platform from pull-based to push-based by investigating Alertmanager
  webhooks automatically. `IncidentStore` (`tools/incident_store.py`)
  persists every incident and its RCA to SQLite, which is also what makes
  the copilot's own history tools answerable.

## Tool inventory

The copilot exposes 19 tools: 16 that read live state or history directly,
2 that run the full LLM investigation pipeline (`investigate_service`,
`generate_runbook`), and 1 that writes (`remediate_scale_deployment`).

## Notes

- Every layer below the Copilot is strictly read-only. The only mutation
  anywhere in the platform is `KubernetesClient.scale_deployment`, reachable
  solely through the approved remediation path.
- Auto-triage automates *notice + investigate*, never *change the cluster* —
  an auto-triaged incident that proposes a fix still parks on
  `awaiting_approval` until a human decides.
- `Copilot -- investigate_service --> Graph` and `Copilot -- direct tools -->
  Agents` both terminate in the same four agents, which is why the copilot
  can answer either a quick fact ("which service has the highest CPU?") or a
  full root-cause question ("why is checkout slow?") depending on how it's
  asked.
- See `ai-platform/copilot/REMEDIATION_RUNBOOK.md` for a live, step-by-step
  walkthrough of the human-approved write path end to end.

## Known gaps

The approval gate is enforced in-process. The HTTP layer in front of it
(`server.py`) has no authentication and binds `0.0.0.0`, so on an untrusted
network the gate is reachable by anyone who can reach the port. Treat this
as a local-development platform until that's addressed.
