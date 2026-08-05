# High p95 Latency

## Symptoms

A service's 95th-percentile request latency has crossed an unacceptable
threshold. `FrontendHighLatency` and `ProductCatalogHighLatency` fire when
p95 latency exceeds 3 seconds for 3 continuous minutes:

```
histogram_quantile(
  0.95,
  sum by (le) (rate(http_server_duration_milliseconds_bucket{service_name="$SERVICE"}[5m]))
)
> 3000
```

Users experience this as a slow-loading storefront, sluggish checkout, or
requests that feel like they're hanging.

## Likely causes

- A downstream call (database, another microservice) has become slow —
  the calling service's own latency is just inheriting it.
- CPU throttling: the pod is hitting its CPU limit and getting throttled by
  the kernel, which shows up as latency rather than errors.
- A traffic spike pushing the service past its comfortable capacity.
- Lock contention or a slow query path triggered by a specific request
  pattern (e.g. an unindexed query only certain inputs hit).

## Diagnostic steps

1. `get_service_metrics($SERVICE)` — check CPU utilization alongside
   latency; sustained CPU near 100% strongly suggests throttling rather
   than a downstream dependency.
2. `get_latency_by_route($SERVICE)` — narrow down *which* endpoint is slow
   rather than assuming the whole service is uniformly affected.
3. `get_slow_traces($SERVICE)` then `get_trace_critical_path(trace_id)` —
   find where in the call chain the time is actually going. A slow
   critical path pointing at a downstream service means the fix belongs
   there, not on the service that merely appears slow.
4. `detect_service_anomalies($SERVICE)` — check whether this is a genuine
   deviation from the service's own baseline or just its normal latency
   profile under current load.

## Remediation

- Downstream dependency is the bottleneck: investigate and fix that
  service instead — scaling the calling service won't help if it's just
  waiting on something else.
- CPU-bound and under-provisioned: `remediate_scale_deployment` to add
  replicas, spreading load across more pods (requires evidence gathered
  above first; guardrails cap the requested count and gate on human
  approval).
- Traffic spike beyond expected capacity: scale proactively rather than
  waiting for the alert to clear on its own, since the underlying load
  isn't going away.

## Escalation

If p95 latency keeps degrading despite additional replicas, the bottleneck
is likely not compute (e.g. a shared datastore, a hard dependency ceiling)
— stop scaling and go back to the trace critical path to find the actual
constraint.
