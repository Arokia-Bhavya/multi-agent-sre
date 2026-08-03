"""Unit tests for the Milestone 6 AlertCorrelator; no cluster needed."""

from unittest import TestCase
from unittest.mock import MagicMock

from alertmanager import AlertmanagerClient
from alert_agent import AlertAgent
from alert_correlator import AlertCorrelator, resolve_alert_service


def _alert(name, severity="critical", started_at=None, labels=None):
    return {
        "name": name,
        "severity": severity,
        "status": "firing",
        "labels": labels or {},
        "annotations": {},
        "started_at": started_at,
    }


class ResolveAlertServiceTests(TestCase):
    def test_prefers_explicit_service_name_label(self):
        alert = _alert("SomeAlert", labels={"service_name": "cart"})
        self.assertEqual(resolve_alert_service(alert), "cart")

    def test_falls_back_to_matching_alert_name_against_known_services(self):
        alert = _alert("FrontendProxyHighErrorRate")
        self.assertEqual(resolve_alert_service(alert), "frontend-proxy")

    def test_returns_none_when_nothing_matches(self):
        alert = _alert("SomeUnrelatedAlert")
        self.assertIsNone(resolve_alert_service(alert))


class AlertCorrelatorTests(TestCase):
    def setUp(self):
        self.alertmanager = AlertmanagerClient()
        self.alert_agent = AlertAgent(self.alertmanager)
        self.correlator = AlertCorrelator(self.alert_agent, correlation_window_seconds=300)

    def test_groups_alerts_for_the_same_service_into_one_incident(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            _alert("CheckoutHighErrorRate", severity="critical",
                   started_at="2024-01-01T00:00:00Z", labels={"service_name": "checkout"}),
            _alert("CheckoutHighLatency", severity="high",
                   started_at="2024-01-01T00:01:00Z", labels={"service_name": "checkout"}),
        ])
        incidents = self.correlator.get_correlated_incidents()
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(incident["service_name"], "checkout")
        self.assertEqual(incident["alert_count"], 2)
        self.assertTrue(incident["is_multi_alert"])
        self.assertEqual(incident["max_severity"], "critical")

    def test_alerts_far_apart_in_time_form_separate_incidents(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            _alert("CheckoutHighErrorRate", started_at="2024-01-01T00:00:00Z",
                   labels={"service_name": "checkout"}),
            _alert("CheckoutHighLatency", started_at="2024-01-01T01:00:00Z",
                   labels={"service_name": "checkout"}),
        ])
        incidents = self.correlator.get_correlated_incidents()
        self.assertEqual(len(incidents), 2)
        self.assertTrue(all(i["alert_count"] == 1 for i in incidents))
        self.assertTrue(all(not i["is_multi_alert"] for i in incidents))

    def test_alerts_for_different_services_never_merge(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            _alert("CheckoutHighErrorRate", started_at="2024-01-01T00:00:00Z",
                   labels={"service_name": "checkout"}),
            _alert("CartHighErrorRate", started_at="2024-01-01T00:00:05Z",
                   labels={"service_name": "cart"}),
        ])
        incidents = self.correlator.get_correlated_incidents()
        self.assertEqual({i["service_name"] for i in incidents}, {"checkout", "cart"})

    def test_incidents_sorted_by_severity_then_alert_count(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            _alert("CartLowSeverity", severity="low", started_at="2024-01-01T00:00:00Z",
                   labels={"service_name": "cart"}),
            _alert("CheckoutCritical", severity="critical", started_at="2024-01-01T00:00:00Z",
                   labels={"service_name": "checkout"}),
        ])
        incidents = self.correlator.get_correlated_incidents()
        self.assertEqual(incidents[0]["service_name"], "checkout")

    def test_unresolvable_service_falls_back_to_unknown_bucket(self):
        self.alertmanager.get_active_alerts = MagicMock(return_value=[
            _alert("SomeUnrelatedAlert", started_at="2024-01-01T00:00:00Z"),
        ])
        incidents = self.correlator.get_correlated_incidents()
        self.assertEqual(incidents[0]["service_name"], "unknown")

    def test_correlate_alerts_accepts_explicit_alert_list(self):
        alerts = [_alert("CheckoutHighErrorRate", labels={"service_name": "checkout"})]
        incidents = self.correlator.correlate_alerts(alerts)
        self.assertEqual(len(incidents), 1)
        self.alert_agent  # sanity: no call to get_active_alerts required here
