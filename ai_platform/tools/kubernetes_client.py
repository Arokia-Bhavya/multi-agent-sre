"""
Kubernetes Client for Multi-Agent SRE Platform

Provides methods to query pod status, deployment health, events, and logs.
"""

from kubernetes import client, config, watch
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone

from ai_platform.tools.pii_redaction import redact_text


class KubernetesClient:
    """
    Client for querying Kubernetes API for pod, deployment, and event information.
    
    Example:
        client = KubernetesClient()
        pods = client.get_pods(namespace="otel-demo")
        events = client.get_recent_events(namespace="otel-demo")
    """
    
    def __init__(self):
        """
        Initialize Kubernetes client using in-cluster config if available,
        otherwise falls back to kubeconfig.
        """
        try:
            # Try in-cluster config first
            config.load_incluster_config()
        except config.config_exception.ConfigException:
            # Fall back to kubeconfig
            config.load_kube_config()
        
        self.v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
    
    def get_pods(self, namespace: str) -> List[Dict[str, Any]]:
        """
        Get all pods in a namespace.
        
        Args:
            namespace: Kubernetes namespace
            
        Returns:
            List of pod dicts with name, status, restarts, age, etc.
        """
        pods = self.v1.list_namespaced_pod(namespace)
        pod_list = []
        for pod in pods.items:
            pod_data = {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "ready": next(
                    (condition.status == "True" for condition in (pod.status.conditions or [])
                     if condition.type == "Ready"),
                    False,
                ),
                "restarts": sum(
                    cs.restart_count or 0 
                    for cs in (pod.status.container_statuses or [])
                ),
                "age_seconds": (
                    (pod.metadata.deletion_timestamp or datetime.now(timezone.utc))
                    - pod.metadata.creation_timestamp
                ).total_seconds() if pod.metadata.creation_timestamp else 0,
            }
            pod_list.append(pod_data)
        
        return pod_list
    
    def get_pod(self, namespace: str, pod_name: str) -> Dict[str, Any]:
        """
        Get detailed information about a specific pod.
        
        Args:
            namespace: Kubernetes namespace
            pod_name: Pod name
            
        Returns:
            Pod dict with detailed status and container info
        """
        pod = self.v1.read_namespaced_pod(pod_name, namespace)
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "status": pod.status.phase,
            "containers": [
                {
                    "name": c.name,
                    "image": c.image,
                    "ready": next(
                        (cs.ready for cs in (pod.status.container_statuses or [])
                         if cs.name == c.name),
                        False
                    ),
                }
                for c in (pod.spec.containers or [])
            ],
            "restarts": sum(
                cs.restart_count or 0
                for cs in (pod.status.container_statuses or [])
            ),
        }
    
    def get_deployments(self, namespace: str) -> List[Dict[str, Any]]:
        """
        Get all deployments in a namespace.
        
        Args:
            namespace: Kubernetes namespace
            
        Returns:
            List of deployment dicts with replica status
        """
        deployments = self.apps_v1.list_namespaced_deployment(namespace)
        deployment_list = []
        for deploy in deployments.items:
            deployment_list.append({
                "name": deploy.metadata.name,
                "namespace": deploy.metadata.namespace,
                "desired_replicas": deploy.spec.replicas or 0,
                "ready_replicas": deploy.status.ready_replicas or 0,
                "updated_replicas": deploy.status.updated_replicas or 0,
                "available_replicas": deploy.status.available_replicas or 0,
            })
        
        return deployment_list
    
    def get_recent_events(
        self,
        namespace: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Get recent Kubernetes events in a namespace.
        
        Args:
            namespace: Kubernetes namespace
            limit: Maximum number of events to return
            
        Returns:
            List of event dicts with type, reason, message, and timestamp
        """
        events = self.v1.list_namespaced_event(namespace, limit=limit)
        event_list = []
        for event in sorted(
            events.items,
            key=lambda e: e.last_timestamp or e.first_timestamp or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )[:limit]:
            event_list.append({
                "type": event.type,
                "reason": event.reason,
                # Event messages are free text (e.g. an image-pull error can
                # embed a registry URL with credentials, a probe failure can
                # echo a response body) and pass through to the LLM, so they
                # get the same PII/secret scrub as pod logs below.
                "message": redact_text(event.message),
                "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                "timestamp": event.last_timestamp or event.first_timestamp,
                "count": event.count or 1,
            })
        
        return event_list
    
    def scale_deployment(
        self,
        namespace: str,
        deployment_name: str,
        replicas: int,
    ) -> Dict[str, Any]:
        """
        Scale a deployment to the given replica count.

        This is the platform's first *write* operation against the cluster —
        every other client method is read-only. Used by the copilot's
        human-approved remediation tool to recover from a deployment that
        was scaled down (accidentally or via fault injection), not for
        general-purpose autoscaling.

        Args:
            namespace: Kubernetes namespace.
            deployment_name: Deployment to scale.
            replicas: Desired replica count.

        Returns:
            Dict with the deployment name and the replica count that was set.
        """
        self.apps_v1.patch_namespaced_deployment_scale(
            name=deployment_name,
            namespace=namespace,
            body={"spec": {"replicas": replicas}},
        )
        return {"name": deployment_name, "namespace": namespace, "replicas": replicas}

    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container: Optional[str] = None,
        tail_lines: int = 100
    ) -> str:
        """
        Get recent logs from a pod container.
        
        Args:
            namespace: Kubernetes namespace
            pod_name: Pod name
            container: Container name (optional, uses first if not specified)
            tail_lines: Number of recent lines to retrieve
            
        Returns:
            Log text, with PII/secret-shaped substrings (emails, phone
            numbers, card numbers, SSNs, API keys/tokens) redacted — this is
            the highest-risk free-text surface in the platform: raw
            application logs from a live e-commerce app, forwarded straight
            to a third-party LLM API and then persisted to disk.
        """
        logs = self.v1.read_namespaced_pod_log(
            pod_name,
            namespace,
            container=container,
            tail_lines=tail_lines,
        )
        return redact_text(logs)
