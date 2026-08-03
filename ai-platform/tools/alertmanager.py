"""
Alert Client for Multi-Agent SRE Platform

Provides methods to query active alerts and classify incidents by severity.

Despite the class name (kept for API compatibility with the Milestone 3
Alert Agent), this client sources alert data from Prometheus's own alerting
API (`/api/v1/alerts`, `/api/v1/rules`) rather than a standalone Alertmanager
service, since the OpenTelemetry Demo Helm deployment does not install
Alertmanager by default.

Note: silencing is a real-Alertmanager-only feature (grouping, inhibition,
routing/notification too) and is not available through this client.
"""

from typing import Dict, List, Any
from enum import Enum

from prometheus_client import PrometheusClient


class AlertSeverity(Enum):
    """Alert severity levels"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class AlertmanagerClient:
    """
    Client for querying active alerts (via Prometheus) and classifying incidents.

    Example:
        client = AlertmanagerClient("http://localhost:9090")
        alerts = client.get_active_alerts()
        critical = client.get_alerts_by_severity(AlertSeverity.CRITICAL)
    """

    def __init__(self, prometheus_url: str = "http://localhost:9090", timeout: float = 15):
        """
        Initialize the alert client.

        Args:
            prometheus_url: Base URL of Prometheus (default: localhost:9090)
            timeout: Request timeout in seconds
        """
        self.prometheus = PrometheusClient(prometheus_url, timeout=timeout)

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        """
        Retrieve all currently active (pending or firing) alerts from Prometheus.

        Returns:
            List of alert dicts with labels, status, and metadata
        """
        alerts = []
        for alert in self.prometheus.get_alerts():
            labels = alert.get("labels", {})
            alerts.append({
                "name": labels.get("alertname"),
                "status": alert.get("state"),
                "severity": labels.get("severity", "unknown"),
                "labels": labels,
                "annotations": alert.get("annotations", {}),
                "started_at": alert.get("activeAt"),
                "ended_at": None,
            })
        return alerts

    def get_alerts_by_severity(
        self,
        severity: AlertSeverity
    ) -> List[Dict[str, Any]]:
        """
        Get active alerts filtered by severity level.

        Args:
            severity: AlertSeverity enum value

        Returns:
            List of alert dicts matching the severity
        """
        all_alerts = self.get_active_alerts()
        return [
            a for a in all_alerts
            if a.get("severity") == severity.value
        ]

    def get_alerts_by_service(
        self,
        service: str
    ) -> List[Dict[str, Any]]:
        """
        Get active alerts for a specific service.

        Args:
            service: Service name to filter by

        Returns:
            List of alert dicts for the service
        """
        all_alerts = self.get_active_alerts()
        return [
            a for a in all_alerts
            if service in a.get("labels", {}).get("job", "")
            or service in a.get("annotations", {}).get("description", "")
        ]

    def classify_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """
        Classify an alert by severity, impact, and likely root cause.

        Args:
            alert: Alert dict from get_active_alerts()

        Returns:
            Classified alert dict with severity, impact, and suggestions
        """
        severity = alert.get("severity", "unknown").lower()

        # Map severity to priority
        severity_priority = {
            "critical": AlertSeverity.CRITICAL,
            "high": AlertSeverity.HIGH,
            "medium": AlertSeverity.MEDIUM,
            "low": AlertSeverity.LOW,
        }

        return {
            **alert,
            "classified_severity": severity_priority.get(
                severity, AlertSeverity.UNKNOWN
            ).value,
            "is_critical": severity == "critical",
            "requires_immediate_action": severity in ["critical", "high"],
        }

    def get_alert_rules(self) -> List[Dict[str, Any]]:
        """
        Retrieve all configured alerting rules from Prometheus (firing or not).

        Returns:
            List of alert rule dicts with name, expr, and state
        """
        return self.prometheus.get_alert_rules()
