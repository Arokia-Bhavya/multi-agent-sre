"""
Alert Correlator for Multi-Agent SRE Platform (Milestone 6).

Groups related active alerts into "incidents" — alerts affecting the same
service that started firing close together in time — instead of presenting
them as an undifferentiated flat list. This is a lightweight, deterministic
correlation layer on top of the Milestone 3 AlertAgent; no LLM is involved.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ai_platform.tools.alert_agent import AlertAgent
from ai_platform.tools.service_matching import DEFAULT_KNOWN_SERVICES, resolve_service_from_alert


SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0, "unknown": -1}


def resolve_alert_service(
    alert: Dict[str, Any], known_services: Optional[List[str]] = None
) -> Optional[str]:
    """
    Best-effort affected-service extraction for an alert.

    Thin alias for `service_matching.py::resolve_service_from_alert`, kept
    under this name for the existing callers/tests. Previously this and
    `coordinator/graph.py::extract_service_name` each held their own copy of
    the service list and matching logic; both now share one implementation,
    so they can no longer drift apart.
    """
    return resolve_service_from_alert(alert, known_services)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class AlertCorrelator:
    """
    Groups active alerts into correlated incidents by affected service and
    time proximity.

    Example:
        correlator = AlertCorrelator(AlertAgent(AlertmanagerClient()))
        for incident in correlator.get_correlated_incidents():
            print(incident["service_name"], incident["alert_count"], incident["max_severity"])
    """

    def __init__(
        self,
        alert_agent: AlertAgent,
        known_services: Optional[List[str]] = None,
        correlation_window_seconds: int = 300,
    ):
        """
        Args:
            alert_agent: Milestone 3 AlertAgent to source active alerts from.
            known_services: Override the default OTel Demo service name list
                used for alert -> service name resolution.
            correlation_window_seconds: Alerts for the same service are
                chained into one incident as long as each next alert starts
                within this many seconds of the running end of the cluster;
                a bigger gap starts a new incident. Default 300s (5 minutes).
        """
        self.alert_agent = alert_agent
        self.known_services = known_services or DEFAULT_KNOWN_SERVICES
        self.correlation_window_seconds = correlation_window_seconds

    def correlate_alerts(
        self, alerts: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """
        Group the given alerts (or, if omitted, all currently active alerts)
        into correlated incidents, sorted by severity then alert count
        (most severe / largest incident first).
        """
        alerts = self.alert_agent.get_active_alerts() if alerts is None else alerts

        by_service: Dict[str, List[Dict[str, Any]]] = {}
        for alert in alerts:
            service = resolve_alert_service(alert, self.known_services) or "unknown"
            by_service.setdefault(service, []).append(alert)

        incidents: List[Dict[str, Any]] = []
        for service, service_alerts in by_service.items():
            incidents.extend(self._cluster_by_time(service, service_alerts))

        incidents.sort(
            key=lambda i: (SEVERITY_RANK.get(i["max_severity"], -1), i["alert_count"]),
            reverse=True,
        )
        return incidents

    def get_correlated_incidents(self) -> List[Dict[str, Any]]:
        """Convenience alias for correlate_alerts() over live active alerts."""
        return self.correlate_alerts()

    def _cluster_by_time(
        self, service: str, alerts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        def sort_key(a: Dict[str, Any]) -> datetime:
            return _parse_timestamp(a.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)

        sorted_alerts = sorted(alerts, key=sort_key)

        clusters: List[List[Dict[str, Any]]] = []
        cluster_end: Optional[datetime] = None
        for alert in sorted_alerts:
            ts = _parse_timestamp(alert.get("started_at"))
            if (
                clusters
                and ts is not None
                and cluster_end is not None
                and (ts - cluster_end).total_seconds() <= self.correlation_window_seconds
            ):
                clusters[-1].append(alert)
                cluster_end = max(cluster_end, ts)
            else:
                clusters.append([alert])
                cluster_end = ts

        return [self._build_incident(service, cluster) for cluster in clusters]

    def _build_incident(self, service: str, alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
        severities = [a.get("severity", "unknown").lower() for a in alerts]
        max_severity = max(severities, key=lambda s: SEVERITY_RANK.get(s, -1), default="unknown")
        timestamps = [t for t in (_parse_timestamp(a.get("started_at")) for a in alerts) if t]

        return {
            "service_name": service,
            "alert_names": [a.get("name") for a in alerts],
            "alert_count": len(alerts),
            "max_severity": max_severity,
            "is_multi_alert": len(alerts) > 1,
            "first_started_at": min(timestamps).isoformat() if timestamps else None,
            "last_started_at": max(timestamps).isoformat() if timestamps else None,
            "alerts": alerts,
        }


if __name__ == "__main__":
    from ai_platform.tools.alertmanager import AlertmanagerClient

    correlator = AlertCorrelator(AlertAgent(AlertmanagerClient("http://localhost:9090")))
    for incident in correlator.get_correlated_incidents():
        print(
            f"{incident['service_name']}: {incident['alert_count']} alert(s), "
            f"max severity={incident['max_severity']}, "
            f"multi-alert={incident['is_multi_alert']}"
        )
