# Realistic end-to-end test: inject a fault, alert fires, copilot investigates, human approves, agent repairs

This walks through the full loop on your **real** Kind cluster: break checkout,
watch it become unhealthy, ask the copilot to investigate, approve its
proposed fix, and confirm it actually repairs the cluster.

Before this, it's worth running the offline demo first (no cluster needed) to
see the approval mechanics in isolation:

```bash
uv run python ai_platform/copilot/sample_remediation_demo.py
```

(Run from the repo root. The script puts the directories it needs on
`sys.path` itself, so no `PYTHONPATH` is required.)

## 0. Prerequisites

- Cluster up, otel-demo namespace healthy:
  ```
  kubectl get pods -n otel-demo
  ```
- Prometheus and Jaeger port-forwarded (separate terminals, leave running):
  ```
  kubectl get svc -n otel-demo
  kubectl port-forward -n otel-demo svc/<prometheus-service> 9090:9090
  kubectl port-forward -n otel-demo svc/<jaeger-service> 16686:16686
  ```
- `.env` has a working `ANTHROPIC_API_KEY` or `GROQ_API_KEY`, plus the URLs
  above and `KUBERNETES_NAMESPACE=otel-demo`.
- Confirm the checkout deployment is healthy before you start:
  ```
  kubectl get deployment checkout -n otel-demo
  ```
  Note its current replica count (almost certainly `1`) — you'll scale back
  to this number later.

## 1. Inject the fault

```
kubectl scale deployment checkout --replicas=0 -n otel-demo
```

Confirm it took effect:
```
kubectl get deployment checkout -n otel-demo
kubectl get pods -n otel-demo | grep checkout
```
You should see `0/0` ready replicas and no checkout pods running.

If your load generator is running (it usually is by default in the
OTel Demo), give it a couple of minutes to hit the checkout flow and start
accumulating errors — this is what would trip `FrontendHighErrorRate` in
Prometheus if enough traffic routes through frontend's checkout path. Either
way, the Kubernetes agent will see the unhealthy deployment immediately,
regardless of whether a named Prometheus alert fires yet.

## 2. Confirm the fault is visible to the platform

Quick sanity check with the existing Milestone 2/3 tooling before involving
the copilot:
```
uv run python ai_platform/tools/test_clients.py
```
Look for `checkout` under the Kubernetes section with `ready_replicas: 0`.

You can also check Prometheus directly for firing alerts:
```
curl -s http://localhost:9090/api/v1/alerts | python3 -m json.tool
```

## 3. Ask the copilot to investigate and fix it

```
uv run python ai_platform/copilot/chat.py
```

Try:
```
you> Why is checkout down?
```
Expect a root-cause answer pointing at the deployment having 0/0 ready
replicas (it may call `investigate_service` and/or `get_deployment_health`
directly).

Then ask it to act:
```
you> Please fix it.
```

The copilot should call `remediate_scale_deployment` and the conversation
will pause with something like:

```
--- APPROVAL REQUIRED ---
  Action:            scale_deployment
  Service:           checkout
  Desired replicas:  1
  Reason:            <the agent's evidence-based justification>
-------------------------
Approve this action? (y/n) >
```

**Nothing has touched the cluster yet at this point.** Verify that yourself
in another terminal if you like:
```
kubectl get deployment checkout -n otel-demo   # still 0/0
```

## 4. Approve (or reject) and verify

Type `y` and press enter. The copilot should report success, and:
```
kubectl get deployment checkout -n otel-demo
kubectl get pods -n otel-demo | grep checkout
```
should now show the deployment scaling back up to its desired replica count
and a new checkout pod starting.

To test the rejection path instead, repeat from step 1, and type `n` at the
prompt — confirm with `kubectl get deployment checkout -n otel-demo` that it's
still at 0/0 afterward.

## 5. Clean up / reset

If replicas don't match what you had before (the agent scales to whatever
`desired_replicas` it proposed, normally 1), reset explicitly:
```
kubectl scale deployment checkout --replicas=1 -n otel-demo
```

## Notes / limitations

- **Only `remediate_scale_deployment` exists today.** It's scoped to exactly
  one action — scaling a deployment to a given replica count — chosen because
  it directly undoes the "scaled to 0" fault above. It is not a general
  auto-remediation system; there's no rollback, pod restart, or config-change
  capability yet (those would be natural Milestone 6 follow-ons).
- **The approval gate is real, not cosmetic.** It's implemented with
  LangGraph's `interrupt()`, which actually pauses graph execution — the
  cluster-mutating code path is unreachable until `respond_to_approval(True, ...)`
  is called. See `ai_platform/copilot/copilot_tools.py::remediate_scale_deployment`
  and `agent.py::SRECopilot.respond_to_approval`.
- **The named Prometheus alerts** (`FrontendHighErrorRate`,
  `FrontendHighLatency`, `FrontendHighMemoryUsage`) are all scoped to the
  `frontend` service specifically, not `checkout` — whether scaling down
  checkout actually trips one of them depends on load-generator traffic and
  how frontend surfaces checkout failures. The Kubernetes agent's own
  unhealthy-deployment detection doesn't depend on any of that, so the
  investigation/repair loop works regardless of whether a named alert fires.
