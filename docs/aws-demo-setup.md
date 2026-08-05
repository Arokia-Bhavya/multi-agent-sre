# AWS Demo Setup — multi-agent-sre on EKS (persistent, through Sunday)

Switched from the single-day EC2+k3s plan to a real EKS cluster since this
needs to stay demoable through Sunday. Good news: this is actually
*simpler* to operate day-to-day than the k3s plan — `kubectl` talks to the
EKS API server directly from your Mac, so there's no SSH tunnel/bastion box
to keep alive. Everything (`helm install`, `kubectl port-forward`, the web
UI/CLI copilot) runs locally on your Mac exactly like your Kind workflow,
just pointed at a different kubeconfig context.

Cost: EKS control plane is a flat $0.10/hr (~$72/mo) regardless of cluster
size, plus a `t3.xlarge` worker node at ~$0.166/hr. Left running
continuously from today (Wed) through Sunday (~4 days / ~96 hrs) that's
roughly $0.10*96 + $0.166*96 ≈ $25-30 total, plus a few dollars of EBS.
Cheap enough to just leave it running — see the "between sessions" note at
the end if you'd rather scale the node down when not in use.

---

## 0. Prerequisites (your Mac)

```bash
brew install eksctl kubectl helm awscli
aws configure   # if not already done — needs an IAM user/role with
                 # EKS, EC2, CloudFormation, and IAM permissions
```

---

## 1. Create the cluster

One command — `eksctl` handles the control plane, VPC, managed node group,
and IAM roles, and writes the kubeconfig context for you automatically.
Takes ~15-20 min (control plane provisioning is the slow part, not
something to route around today).

```bash
eksctl create cluster \
  --name sre-demo \
  --region us-east-1 \
  --version 1.31 \
  --nodegroup-name sre-workers \
  --node-type t3.xlarge \
  --nodes 1 \
  --nodes-min 1 \
  --nodes-max 1 \
  --managed
```

Verify:

```bash
kubectl get nodes
kubectl config current-context   # should now point at the sre-demo cluster
```

If you also work against your local Kind cluster, switch back and forth
with `kubectl config use-context <name>` — `kubectl config get-contexts`
lists both.

---

## 2. Deploy otel-demo (identical to your existing steps)

From your repo root, targeting the EKS context:

```bash
cd /Users/arokiabhavya/code/multi-agent-sre

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

helm install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo --create-namespace \
  --values observability/helm/otel-demo-minimal-values.yaml

kubectl get pods -n otel-demo -w   # wait for everything Running, ~3-5 min
```

---

## 3. Run ai_platform — locally, same as your normal workflow

No dockerization, no bastion box. Your existing local `.env` already works
since `PROMETHEUS_URL`/`JAEGER_URL` just point at whatever's
port-forwarded, and `KUBERNETES_NAMESPACE`/remediation actions go through
whatever kubeconfig context is active — which is now `sre-demo` (EKS)
instead of Kind.

```bash
kubectl port-forward -n otel-demo svc/frontend-proxy 8080:8080 &
kubectl port-forward -n otel-demo svc/prometheus 9090:9090 &
kubectl port-forward -n otel-demo svc/jaeger 16686:16686 &
kubectl port-forward -n otel-demo svc/otel-demo-alertmanager 9093:9093 &

uv sync
uv run python -m ai_platform.webui.server
# or: uv run python -m ai_platform.copilot.chat
```

Same `localhost:8080` / `:8000` / `:9090` / `:16686` URLs as always — Google
OAuth redirect (`http://localhost:8000/auth/callback`) needs no changes
since the web UI is still bound to `127.0.0.1` and reached through
`kubectl port-forward`, not a public IP.

---

## 4. Stop after today's recording, restart Sunday

You said the plan is: deploy today, record, stop; come back Sunday and
start again. Don't run `eksctl delete cluster` for that — deleting tears
down the whole control plane/VPC and you'd eat the full ~15-20 min
`eksctl create cluster` wait again on Sunday. Instead, scale the node
group to 0. The control plane stays intact (and keeps billing its flat
$0.10/hr regardless — that's unavoidable short of deleting), but the
~$0.166/hr worker-node cost stops, and Sunday's restart is ~2-3 minutes
instead of ~20.

**Today, right after you finish recording:**

```bash
eksctl scale nodegroup --cluster sre-demo --name sre-workers --nodes 0
```

This evicts all otel-demo/ai_platform pods — expected, nothing to worry
about. Nothing here is stateful beyond postgresql/valkey, which use
ephemeral storage in the minimal profile, so there's no data to lose.

**Sunday, before you start recording again:**

```bash
eksctl scale nodegroup --cluster sre-demo --name sre-workers --nodes 1
kubectl get pods -n otel-demo -w   # wait for everything to reschedule and go Running, ~2-3 min
```

Then redo step 3 (port-forwards + `uv run`) — nothing else needs
reinstalling; the cluster, namespace, and Helm release are all still
there.

**Cost for this pattern:** control plane $0.10/hr × ~96 hrs (Wed→Sun,
running continuously) ≈ $9.60, plus worker-node cost only for the hours
you're actually actively demoing (today's session + Sunday's session,
maybe 2-4 hrs total) ≈ $0.33-0.66. Call it ~$10-12 total for the whole
week, versus ~$25-30 if you'd left the node running throughout.

---

## 5. Teardown (after Sunday)

```bash
eksctl delete cluster --name sre-demo --region us-east-1
```

This tears down the control plane, node group, and the VPC/networking
eksctl created — confirm in the AWS console afterward that no orphaned
EBS volumes or load balancers were left behind (rare, but worth a 30s
check given EKS creates its own VPC by default).
