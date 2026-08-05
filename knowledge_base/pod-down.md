# Pod Down / No Metrics Reporting

## Symptoms

A service has stopped reporting container metrics entirely — not "slow" or
"erroring," but absent. `FrontendPodDown` and `ProductCatalogPodDown` fire
when `absent()` on that container's cAdvisor metric is true for 1 minute:

```
absent(container_memory_usage_bytes{namespace="otel-demo",container="$SERVICE"})
```

This is a critical-severity pattern: the service isn't degraded, it's not
running at all (scaled to 0, crash-looping badly enough that no metrics
sample ever lands, or the node it was on is gone).

## Likely causes

- Deployment was deliberately or accidentally scaled to 0 replicas.
- The pod is crash-looping fast enough (e.g. `CrashLoopBackOff` with a
  short back-off) that cAdvisor never captures a steady-state sample.
- A bad image or config change causing the container to fail on startup
  every time (check `kubectl describe pod` for the exact failure reason —
  `ImagePullBackOff`, `CreateContainerConfigError`, a failing init
  container, etc.).
- Node-level failure or eviction (OOM at the node level, disk pressure)
  taking the pod down independent of anything in the container itself.

## Diagnostic steps

1. `get_deployment_health($SERVICE)` — confirm ready vs. desired replica
   count; 0/N ready (or the deployment missing desired replicas entirely)
   confirms this isn't a transient metrics gap.
2. `get_pod_health($SERVICE)` — check for unhealthy pods and high restart
   counts, which distinguishes "scaled to 0" (no pods at all) from
   "crash-looping" (pods exist, restarting repeatedly).
3. `get_recent_k8s_events($SERVICE)` — scheduling failures, image pull
   errors, OOMKills, and failed liveness/readiness probes all show up here
   and usually explain the failure directly.
4. `get_pod_logs(pod_name)` — if a pod exists (even briefly, mid
   crash-loop), its last log lines before exit are often the fastest path
   to root cause.
5. Equivalent manual check: `kubectl get pods -n otel-demo -l app=$SERVICE`
   and `kubectl describe pod <pod> -n otel-demo`.

## Remediation

- Scaled to 0 (deliberately or by mistake): `remediate_scale_deployment`
  back to a healthy replica count (requires evidence gathered above first;
  guardrails cap the requested count and gate on human approval).
- Crash-looping on a bad deploy: roll back the image/config rather than
  scaling — restarting more copies of a container that fails on startup
  just produces more crash loops.
- Node-level failure: this is outside what `remediate_scale_deployment`
  fixes directly; Kubernetes should reschedule automatically once the node
  recovers, but a pod stuck `Pending` with no eligible node needs cluster-
  capacity attention, not an application-level fix.

## Escalation

A pod-down alert on a service other services depend on (e.g.
`product-catalog`, which `frontend` and `checkout` both call) is likely to
also trigger downstream `HighErrorRate` alerts on those callers — check
`get_correlated_incidents` to confirm they're one incident, not
independent problems, before working them separately.
