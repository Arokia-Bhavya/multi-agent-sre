"""
Alert Agent for Multi-Agent SRE Platform

Answers questions about active alerts: reading them, classifying incidents,
determining severity, and deciding where an alert should be routed.

Note: routing here means "which channel this alert *should* go to" — it
returns a route label, it does not actually deliver notifications. Real
Slack/Teams delivery is planned for Milestone 6.
"""

from typing import Dict, List, Any

from alertmanager import AlertmanagerClient, AlertSeverity


class AlertAgent:
    """
    Agent responsible for reading, classifying, and routing active alerts
    using the AlertmanagerClient (Prometheus-backed).

    Example:
        agent = AlertAgent(AlertmanagerClient("http://localhost:9090"))
        agent.get_active_alerts()
        agent.classify_incidents()
        agent.get_incident_summary()
    """

    # Suggested notification channel per severity. Placeholder mapping used
    # for routing decisions until real notification delivery (Milestone 6).
    SEVERITY_ROUTES = {
        "critical": "pagerduty",
        "high": "slack-incidents",
        "medium": "slack-alerts",
        "low": "log-only",
        "unknown": "log-only",
    }

    def __init__(self, alertmanager_client: AlertmanagerClient):
        self.alerts = alertmanager_client

    def get_active_alerts(self) -> List[Dict[str, Any]]:
        """
        All currently active (pending or firing) alerts.
        """
        return self.alerts.get_active_alerts()

    def get_critical_alerts(self) -> List[Dict[str, Any]]:
        """
        Active alerts with severity=critical.
        """
        return self.alerts.get_alerts_by_severity(AlertSeverity.CRITICAL)

    def get_alerts_for_service(self, service: str) -> List[Dict[str, Any]]:
        """
        Active alerts related to a specific service.
        """
        return self.alerts.get_alerts_by_service(service)

    def get_all_alert_rules(self) -> List[Dict[str, Any]]:
        """
        All configured alerting rules and their current state
        (inactive/pending/firing), regardless of whether they're currently
        active. Unlike get_active_alerts() (which only returns pending/firing
        alerts from Prometheus's /api/v1/alerts), this reads /api/v1/rules so
        a dashboard can show every rule's state, including healthy
        (inactive) ones.
        """
        return self.alerts.get_alert_rules()

    def determine_severity(self, alert: Dict[str, Any]) -> str:
        """
        Normalized severity string for an alert (critical/high/medium/low/unknown).
        """
        return alert.get("severity", "unknown").lower()

    def classify_incidents(self) -> List[Dict[str, Any]]:
        """
        All active alerts, each annotated with classification metadata
        (classified_severity, is_critical, requires_immediate_action).
        """
        return [self.alerts.classify_alert(alert) for alert in self.get_active_alerts()]

    def route_alert(self, alert: Dict[str, Any]) -> str:
        """
        Suggested notification channel for an alert, based on severity.
        """
        severity = self.determine_severity(alert)
        return self.SEVERITY_ROUTES.get(severity, "log-only")

    def get_incident_summary(self) -> Dict[str, Any]:
        """
        Combined snapshot of current incidents: totals by criticality and
        suggested routing per alert.
        """
        classified = self.classify_incidents()
        return {
            "total_active": len(classified),
            "critical_count": sum(1 for a in classified if a["is_critical"]),
            "requires_immediate_action": sum(
                1 for a in classified if a["requires_immediate_action"]
            ),
            "routes": {a["name"]: self.route_alert(a) for a in classified},
        }


if __name__ == "__main__":
    agent = AlertAgent(AlertmanagerClient("http://localhost:9090"))

    print("Incident summary:")
    print(agent.get_incident_summary())
