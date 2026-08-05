# High Memory Usage

## Symptoms

A container's working-set memory has crossed a sustained threshold.
`FrontendHighMemoryUsage` and `ProductCatalogHighMemoryUsage` fire when a
container uses more than 200 MiB of working-set memory for 5 continuous
minutes:

```
sum by (pod, container) (
  container_memory_working_set_bytes{namespace="otel-demo",container="$SERVICE"}
) > 209715200
```

This is a leading indicator, not necessarily an active outage yet — left
unaddressed, it typically progresses to an OOMKill and then a pod-down
event (see `pod-down.md`).

## Likely causes

- A genuine memory leak: usage climbs monotonically over time rather than
  plateauing, visible as a steady upward trend in `get_service_metrics`
  history rather than a one-time step change.
- A traffic-driven working set: memory tracks request volume (caches,
  in-flight request buffers) and is expected to be higher under load —
  distinguish this from a leak by checking whether it falls back down when
  traffic drops.
- A single expensive request or batch operation holding onto memory
  disproportionately (e.g. loading a large result set into memory at
  once).
- The container's memory limit itself is simply set too low for its normal
  working set, making this a sizing problem rather than a bug.

## Diagnostic steps

1. `get_service_metrics($SERVICE)` — check the current memory figure
   against the 200 MiB threshold and note request rate at the same time,
   to separate "leak" from "load-driven."
2. `detect_service_anomalies($SERVICE)` — a genuine leak shows as a
   deviation from the service's own historical baseline, not just a value
   above a static threshold.
3. `get_recent_k8s_events($SERVICE)` — look for prior OOMKill events on
   this container, which confirm this has already crossed the line once
   before.
4. `get_pod_logs(pod_name)` — some services log their own memory-pressure
   warnings before an OOMKill; worth checking even though this is a
   metrics-driven alert.

## Remediation

- Confirmed leak: this needs a code fix (or, as an immediate mitigation,
  scheduled pod restarts to bound the impact) — scaling out doesn't fix a
  per-pod leak, it just means more pods leaking independently.
- Load-driven and within expected bounds: `remediate_scale_deployment` to
  add replicas and spread request volume, reducing per-pod working set
  (requires evidence gathered above first; guardrails cap the requested
  count and gate on human approval).
- Limit set too low for genuinely normal usage: this is a configuration
  change (raising the container's memory limit/request), not something
  `remediate_scale_deployment` addresses — flag for a manual chart/values
  update.

## Escalation

If memory keeps climbing after a restart-based mitigation, treat it as a
confirmed leak requiring a code-level fix, not a capacity problem —
repeated scaling or restarting without addressing the leak just delays the
next OOMKill.
