# Elevated 5xx / Error Rate

## Symptoms

A service is returning an unusually high share of 5xx responses to its
callers. In this platform, `FrontendHighErrorRate` and
`ProductCatalogHighErrorRate` fire when more than 5% of a service's
requests return 5xx for 2 continuous minutes:

```
sum(rate(http_server_duration_milliseconds_count{service_name="$SERVICE",http_status_code=~"5.."}[5m]))
/
sum(rate(http_server_duration_milliseconds_count{service_name="$SERVICE"}[5m]))
> 0.05
```

Users typically notice this as failed page loads, failed checkouts, or
"something went wrong" errors in the storefront.

## Likely causes

- A downstream dependency the service calls is unavailable, slow, or
  itself erroring (cascading failure — check the service's own
  dependencies before assuming the fault is local).
- The service's own pods are unhealthy or crash-looping, so a fraction of
  requests hit a pod that's about to be evicted or already failing
  readiness.
- A recent deployment introduced a bug (correlate the alert's start time
  against the most recent rollout).
- Resource exhaustion (CPU throttling, OOM) causing request handling to
  fail rather than merely slow down.

## Diagnostic steps

1. `get_service_metrics($SERVICE)` — confirm the current error rate and
   check request rate alongside it (a spike in errors with no change in
   traffic points at the service itself; errors that scale with a traffic
   spike point at capacity).
2. `get_slow_traces($SERVICE)` / `get_trace_critical_path(trace_id)` — find
   which downstream call, if any, the errors trace back to.
3. `get_pod_health($SERVICE)` and `get_recent_k8s_events($SERVICE)` — rule
   out crash-looping or scheduling failures as the underlying cause.
4. `get_pod_logs(pod_name)` on an affected pod — the actual error/stack
   trace usually narrows this immediately.
5. Equivalent manual check: `kubectl logs -n otel-demo <pod> --tail=200`
   and `kubectl get events -n otel-demo --field-selector involvedObject.name=<pod>`.

## Remediation

- If root cause is a specific unhealthy dependency: fix or restart that
  dependency, not the erroring service itself.
- If root cause is under-provisioned replicas failing to keep up:
  `remediate_scale_deployment` to increase replica count (requires
  evidence gathered above first; guardrails cap the requested count and
  gate on human approval).
- If root cause is a bad deployment: roll back to the previous image/
  revision rather than scaling — scaling a bad build just runs more copies
  of the bug.

## Escalation

If error rate keeps climbing after remediation, or the root cause implicates
a shared dependency affecting multiple services, treat it as a wider
incident rather than a single-service fix — check
`get_correlated_incidents` for other alerts firing around the same time.
