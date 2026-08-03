"""
Kubernetes Agent for Multi-Agent SRE Platform

Answers questions about workload health for services in the OpenTelemetry
Demo namespace: pod status, deployment health, recent events, restarts, and
logs, using the Kubernetes API.
"""

from typing import Dict, List, Any, Optional

from kubernetes_client import KubernetesClient


class KubernetesAgent:
    """
    Agent responsible for answering Kubernetes workload-health questions
    using the KubernetesClient.

    Example:
        agent = KubernetesAgent(KubernetesClient())
        agent.get_pod_status("otel-demo")
        agent.get_unhealthy_pods("otel-demo")
        agent.get_deployment_health("otel-demo")
        agent.get_recent_events("otel-demo")
    """

    def __init__(self, kubernetes_client: KubernetesClient):
        self.k8s = kubernetes_client

    def get_pod_status(self, namespace: str) -> List[Dict[str, Any]]:
        """
        All pods in the namespace with status, readiness, and restart counts.
        """
        return self.k8s.get_pods(namespace)

    def get_unhealthy_pods(self, namespace: str) -> List[Dict[str, Any]]:
        """
        Pods that are not Running or not passing their readiness probe.
        """
        return [
            pod for pod in self.get_pod_status(namespace)
            if pod["status"] != "Running" or not pod["ready"]
        ]

    def get_high_restart_pods(self, namespace: str, threshold: int = 3) -> List[Dict[str, Any]]:
        """
        Pods whose restart count is at or above `threshold` — a common
        symptom of crash-looping.
        """
        return [pod for pod in self.get_pod_status(namespace) if pod["restarts"] >= threshold]

    def get_deployment_health(self, namespace: str) -> List[Dict[str, Any]]:
        """
        All deployments in the namespace, annotated with a `healthy` flag
        (ready_replicas matches desired_replicas, and at least one replica
        is desired).
        """
        deployments = self.k8s.get_deployments(namespace)
        return [
            {
                **deployment,
                "healthy": (
                    deployment["desired_replicas"] > 0
                    and deployment["ready_replicas"] == deployment["desired_replicas"]
                ),
            }
            for deployment in deployments
        ]

    def get_pod_health(self, namespace: str, restart_threshold: int = 3) -> Dict[str, List[Dict[str, Any]]]:
        """
        Combined pod-health check: unhealthy pods and high-restart pods,
        computed from a single `get_pod_status` fetch.

        `get_unhealthy_pods` and `get_high_restart_pods` each call
        `get_pod_status` independently, which means calling both back-to-back
        (as the copilot's `get_pod_health` tool used to) issued two separate
        `list_namespaced_pod` API calls for the exact same data. This does
        the fetch once and derives both from it.
        """
        pods = self.get_pod_status(namespace)
        return {
            "unhealthy_pods": [p for p in pods if p["status"] != "Running" or not p["ready"]],
            "high_restart_pods": [p for p in pods if p["restarts"] >= restart_threshold],
        }

    def get_unhealthy_deployments(self, namespace: str) -> List[Dict[str, Any]]:
        """
        Deployments that are not fully available (ready_replicas < desired_replicas).
        """
        return [d for d in self.get_deployment_health(namespace) if not d["healthy"]]

    def get_recent_events(self, namespace: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Most recent Kubernetes events in the namespace.
        """
        return self.k8s.get_recent_events(namespace, limit=limit)

    def get_warning_events(self, namespace: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Recent events of type "Warning" only (scheduling failures, probe
        failures, image pull errors, etc).
        """
        events = self.k8s.get_recent_events(namespace, limit=limit * 2)
        return [e for e in events if e.get("type") == "Warning"][:limit]

    def scale_deployment(self, namespace: str, deployment_name: str, replicas: int) -> Dict[str, Any]:
        """
        Scale a deployment to the given replica count. This is a *write*
        action — used by the copilot's human-approved remediation tool to
        recover a deployment that was scaled down, not for routine use.
        """
        return self.k8s.scale_deployment(namespace, deployment_name, replicas)

    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container: Optional[str] = None,
        tail_lines: int = 100,
    ) -> str:
        """
        Recent log lines from a pod's container.
        """
        return self.k8s.get_pod_logs(namespace, pod_name, container=container, tail_lines=tail_lines)

    def get_namespace_summary(self, namespace: str) -> Dict[str, Any]:
        """
        Combined health snapshot for a namespace: pod/deployment counts and
        problem counts.
        """
        pods = self.get_pod_status(namespace)
        return {
            "namespace": namespace,
            "total_pods": len(pods),
            "unhealthy_pods": len(self.get_unhealthy_pods(namespace)),
            "high_restart_pods": len(self.get_high_restart_pods(namespace)),
            "unhealthy_deployments": len(self.get_unhealthy_deployments(namespace)),
        }


if __name__ == "__main__":
    agent = KubernetesAgent(KubernetesClient())

    print("Namespace summary:")
    print(agent.get_namespace_summary("otel-demo"))

    print("\nUnhealthy pods:")
    for pod in agent.get_unhealthy_pods("otel-demo"):
        print(f"  {pod['name']}: status={pod['status']} ready={pod['ready']}")
