# Demo Recording Script — multi-agent-sre on AWS

Assumes `aws-demo-setup.md` is done: EC2 box up, otel-demo running, tunnel
open, web UI (or CLI copilot) reachable at `localhost:8000`. Total run time
~6-8 min — trim narration to taste.

---

## Before you hit record

- Storefront loads at `localhost:8080`
- Web UI loads at `localhost:8000` and you can sign in (or have the CLI
  copilot ready in a terminal instead)
- Prometheus rules loaded: `curl localhost:9090/api/v1/rules` shows
  `FrontendHighErrorRate`, `FrontendHighLatency`, `FrontendHighMemoryUsage`,
  `ProductCatalogPodDown`
- Flagd UI reachable — confirm the exact service name first
  (`kubectl get svc -n otel-demo | grep flagd`, typically `otel-demo-flagd`
  with the UI on a named port, or a separate `-flagd-ui` service depending
  on chart version), then port-forward it, e.g.:
  `kubectl port-forward -n otel-demo svc/otel-demo-flagd 8081:8080 &`
  then `localhost:8081`

---

## 1. Set the scene (30s)

Show the storefront at `localhost:8080` — a normal, working e-commerce app.
One line of narration: "This is the Astronomy Shop, a realistic
microservices app instrumented with OpenTelemetry, running on Kubernetes on
AWS. My platform watches it and investigates incidents autonomously."

## 2. Show the architecture briefly (20s)

Optional: a quick look at `kubectl get pods -n otel-demo` in a terminal —
~20 services running — grounds "this is a real distributed system," not a
toy.

## 3. Inject a real failure (30s)

Open the Flagd UI (`localhost:8081`). Toggle one of the demo's built-in
fault flags — good options that map cleanly to your alert rules:

- `productCatalogFailure` → triggers `ProductCatalogPodDown`-style errors
- `cartFailure` → cart service errors, feeds into `FrontendHighErrorRate`
- `adHighCpu` → CPU pressure
- `kafkaQueueProblems` — skip, kafka is disabled in your minimal profile

Flip it on. Narrate: "I'm injecting a real fault into the product catalog
service — this is the same kind of failure mode a production incident
looks like."

## 4. Let the platform catch it (30-90s, can cut in editing)

Alert fires in Prometheus → Alertmanager → your webhook → auto-triage
kicks off. Show the Web UI's **Operations Hub** or **Timeline** tab
picking up the new incident (or, if using the CLI, ask the copilot
directly — see step 5b).

## 5a. Web UI walkthrough (2 min)

- **Timeline** tab: click into the new incident, show the investigation
  trace (tool calls: Prometheus query, Jaeger trace lookup, correlated
  alerts)
- Show the generated **RCA** — narrate that it's grounded in the actual
  metrics/traces pulled, not a canned response
- Show the generated **remediation runbook**
- Trigger the approval modal on a remediation action (e.g. scale a
  deployment) — show that a human has to approve before anything touches
  the cluster. This is the single most important beat for an "SRE
  platform" demo: autonomy with a human gate on mutation.

## 5b. CLI copilot walkthrough (alternative/addition, 2 min)

```bash
uv run python -m ai_platform.copilot.chat
```

Ask, in order:
- "Show me the latest critical alerts"
- "Why is the product catalog service failing?" (or whichever flag you
  flipped)
- "Give me a runbook to fix this"
- "Has this happened before?" — shows the semantic search / incident
  history tool

Let each tool-call trace print — that's the "show your work" moment that
makes this feel like a real agent, not a chatbot.

## 6. Resolve and close the loop (30s)

Flip the Flagd flag back off. Show the incident resolving (or narrate that
it would auto-detect recovery) and, if you triggered remediation, show
the approved action actually applied (`kubectl get deploy -n otel-demo` —
replica count changed).

## 7. Wrap (15s)

One line: "All of this — detection, investigation, RCA, runbook, and
human-gated remediation — happened autonomously against a live cluster,
here running on AWS." Cut.

---

## Fallback if something's flaky on recording day

The repo has fully offline, deterministic demo scripts that need no
cluster or API key — good as a backup or a supplement if a live tool call
is slow/fails on camera:

```bash
uv run python -m ai_platform.copilot.sample_demo
uv run python -m ai_platform.copilot.sample_remediation_demo
```
