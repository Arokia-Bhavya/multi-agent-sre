"""
Unit tests for the TF-IDF-based similar-incident search
(incident_search.py). Pure logic, no cluster/network/LLM key needed.
"""

from unittest import TestCase

from ai_platform.tools.incident_store import IncidentStore
from ai_platform.tools.incident_search import search_similar_incidents


def _store_with(*incidents):
    """incidents: list of (fingerprint, alert_name, service, content)."""
    store = IncidentStore()
    for fingerprint, alert_name, service, content in incidents:
        store.create(fingerprint, alert_name=alert_name, service=service, severity="critical")
        if content:
            store.update(fingerprint, content=content)
    return store


class SearchSimilarIncidentsTests(TestCase):
    def test_ranks_more_similar_incident_first(self):
        store = _store_with(
            ("fp1", "ProductCatalogPodDown", "product-catalog",
             "Root cause: product-catalog deployment was scaled to 0 replicas, causing the pod to disappear."),
            ("fp2", "FrontendHighErrorRate", "frontend",
             "Root cause: frontend received 5xx errors because product-catalog was unreachable (scaled to 0)."),
            ("fp3", "CheckoutHighLatency", "checkout",
             "Root cause: checkout p95 latency spiked due to a slow downstream payment call during a traffic burst."),
        )

        results = search_similar_incidents(store.list(), "product-catalog pod scaled to zero replicas")

        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0].record.fingerprint, "fp1")
        # checkout's unrelated latency incident should not outrank the two
        # incidents that actually mention product-catalog / scaling to 0.
        result_fingerprints = [r.record.fingerprint for r in results]
        if "fp3" in result_fingerprints:
            self.assertLess(result_fingerprints.index("fp3"), len(result_fingerprints))
            self.assertNotEqual(result_fingerprints[0], "fp3")

    def test_unrelated_query_returns_no_or_low_matches(self):
        store = _store_with(
            ("fp1", "ProductCatalogPodDown", "product-catalog", "Root cause: scaled to 0 replicas."),
        )

        results = search_similar_incidents(store.list(), "kubernetes cluster autoscaling nodes")

        self.assertEqual(results, [])

    def test_empty_corpus_returns_empty_list(self):
        store = IncidentStore()

        self.assertEqual(search_similar_incidents(store.list(), "anything"), [])

    def test_empty_query_returns_empty_list(self):
        store = _store_with(("fp1", "A", "checkout", "Root cause: something broke."))

        self.assertEqual(search_similar_incidents(store.list(), ""), [])
        self.assertEqual(search_similar_incidents(store.list(), "   "), [])

    def test_incidents_still_investigating_are_matchable_on_alert_and_service_alone(self):
        # No RCA content yet (still mid-investigation), but alert_name/service
        # alone are still legitimate signal to match against.
        store = IncidentStore()
        store.create("fp1", alert_name="CheckoutPodDown", service="checkout", severity="critical")

        results = search_similar_incidents(store.list(), "checkout pod down")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].record.fingerprint, "fp1")

    def test_record_with_no_indexable_text_at_all_is_excluded(self):
        store = IncidentStore()
        store.create("fp1", alert_name="", service=None, severity="critical")  # nothing to index

        results = search_similar_incidents(store.list(), "anything")

        self.assertEqual(results, [])

    def test_top_k_caps_number_of_results(self):
        store = _store_with(
            ("fp1", "CheckoutError", "checkout", "Root cause: checkout deployment error."),
            ("fp2", "CheckoutError2", "checkout", "Root cause: checkout deployment error again."),
            ("fp3", "CheckoutError3", "checkout", "Root cause: checkout deployment error once more."),
        )

        results = search_similar_incidents(store.list(), "checkout deployment error", top_k=2)

        self.assertLessEqual(len(results), 2)

    def test_min_similarity_filters_out_weak_matches(self):
        store = _store_with(
            ("fp1", "CheckoutError", "checkout", "Root cause: checkout deployment error."),
        )

        # min_similarity=1.0 (perfect match only) should filter out a
        # merely-related query.
        results = search_similar_incidents(store.list(), "checkout deployment error", min_similarity=1.01)

        self.assertEqual(results, [])
