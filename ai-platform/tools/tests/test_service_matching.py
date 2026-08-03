"""
Unit tests for alert -> service resolution (`tools/service_matching.py`).

Pure logic; no cluster, network, or API key required.

The `InfrastructureAlertsResolveToNone` class is the regression suite for the
substring-matching bug: every alert name in it is a real stock
kube-prometheus-stack alert that the old matcher attributed to an unrelated
OTel Demo service, which then triggered a full LLM investigation into the
wrong thing.
"""

from unittest import TestCase

from service_matching import (
    DEFAULT_KNOWN_SERVICES,
    match_service_name,
    resolve_service_from_alert,
    tokenize,
)


class TokenizeTests(TestCase):
    def test_splits_camel_case(self):
        self.assertEqual(
            tokenize("FrontendProxyHighLatency"), ["frontend", "proxy", "high", "latency"]
        )

    def test_keeps_acronyms_intact(self):
        self.assertEqual(tokenize("CPUThrottlingHigh"), ["cpu", "throttling", "high"])

    def test_splits_hyphens_and_underscores(self):
        self.assertEqual(tokenize("product-catalog"), ["product", "catalog"])
        self.assertEqual(tokenize("frontend_proxy"), ["frontend", "proxy"])

    def test_empty_name_yields_no_tokens(self):
        self.assertEqual(tokenize(""), [])


class InfrastructureAlertsResolveToNone(TestCase):
    """
    Regression: the previous matcher squashed the alert name to
    lowercase-alphanumeric and asked whether a service name appeared anywhere
    inside it, so the two-letter service "ad" matched the "ad" in "ready",
    "load", and "reload". These must all resolve to None — they're node- and
    Alertmanager-level alerts, not application service alerts.
    """

    def test_kube_node_not_ready_does_not_match_ad(self):
        self.assertIsNone(match_service_name("KubeNodeNotReady"))

    def test_high_load_average_does_not_match_ad(self):
        self.assertIsNone(match_service_name("HighLoadAverage"))

    def test_alertmanager_failed_reload_does_not_match_ad(self):
        self.assertIsNone(match_service_name("AlertmanagerFailedReload"))

    def test_assorted_kube_alerts_resolve_to_none(self):
        for alert_name in [
            "TargetDown",
            "Watchdog",
            "InfoInhibitor",
            "KubeletDown",
            "KubePodCrashLooping",
            "KubeDeploymentReplicasMismatch",
            "NodeMemoryHighUtilization",
            "EtcdHighFsyncDurations",
            "CPUThrottlingHigh",
            "KubeAPIErrorBudgetBurn",
        ]:
            with self.subTest(alert=alert_name):
                self.assertIsNone(match_service_name(alert_name))


class MatchServiceNameTests(TestCase):
    def test_matches_multi_token_service(self):
        self.assertEqual(match_service_name("ProductCatalogPodDown"), "product-catalog")

    def test_prefers_more_specific_service(self):
        """"frontend-proxy" must win over "frontend" when both could match."""
        self.assertEqual(match_service_name("FrontendProxyHighLatency"), "frontend-proxy")

    def test_falls_back_to_shorter_service(self):
        self.assertEqual(match_service_name("FrontendHighErrorRate"), "frontend")

    def test_matches_squashed_multi_word_service(self):
        """An alert that runs a hyphenated service together as one word."""
        self.assertEqual(match_service_name("LoadgeneratorDown"), "load-generator")

    def test_matches_short_service_on_a_real_word_boundary(self):
        """"ad" should still match when it is genuinely its own word."""
        self.assertEqual(match_service_name("AdHighErrorRate"), "ad")

    def test_honours_custom_known_services(self):
        self.assertEqual(match_service_name("WidgetDown", ["widget"]), "widget")
        self.assertIsNone(match_service_name("CheckoutDown", ["widget"]))

    def test_empty_known_services_matches_nothing(self):
        self.assertIsNone(match_service_name("CheckoutDown", []))

    def test_every_known_service_matches_its_own_name(self):
        """Sanity check that no service is unreachable by the matcher."""
        for service in DEFAULT_KNOWN_SERVICES:
            with self.subTest(service=service):
                self.assertEqual(match_service_name(f"{service}-down"), service)


class ResolveServiceFromAlertTests(TestCase):
    def test_prefers_explicit_service_name_label(self):
        alert = {"name": "AnythingAtAll", "labels": {"service_name": "checkout"}}
        self.assertEqual(resolve_service_from_alert(alert), "checkout")

    def test_falls_back_to_service_label(self):
        alert = {"name": "AnythingAtAll", "labels": {"service": "cart"}}
        self.assertEqual(resolve_service_from_alert(alert), "cart")

    def test_falls_back_to_job_label(self):
        alert = {"name": "AnythingAtAll", "labels": {"job": "shipping"}}
        self.assertEqual(resolve_service_from_alert(alert), "shipping")

    def test_falls_back_to_alert_name_when_no_labels(self):
        alert = {"name": "CheckoutHighLatency", "labels": {}}
        self.assertEqual(resolve_service_from_alert(alert), "checkout")

    def test_label_wins_even_for_an_infrastructure_alert(self):
        """An explicit label is authoritative and bypasses name matching."""
        alert = {"name": "KubeNodeNotReady", "labels": {"service_name": "cart"}}
        self.assertEqual(resolve_service_from_alert(alert), "cart")

    def test_returns_none_for_unmatched_alert(self):
        self.assertIsNone(resolve_service_from_alert({"name": "KubeNodeNotReady", "labels": {}}))

    def test_handles_missing_name_and_labels(self):
        self.assertIsNone(resolve_service_from_alert({}))
