"""
Unit tests for the SQLite-backed IncidentStore (Milestone 6 persistence
extension). No cluster, network, or LLM key needed — everything here is
plain sqlite3, mostly against ":memory:" databases, plus one test that
exercises a real on-disk file to confirm restart survival.
"""

import os
import tempfile
from unittest import TestCase

from incident_store import IncidentRecord, IncidentStore


class IncidentStoreBasicsTests(TestCase):
    def setUp(self):
        self.store = IncidentStore()  # ":memory:" by default

    def test_create_then_get_round_trips(self):
        created = self.store.create("fp1", alert_name="CheckoutHighErrors", service="checkout", severity="critical")

        fetched = self.store.get("fp1")

        self.assertIsInstance(created, IncidentRecord)
        self.assertEqual(fetched.fingerprint, "fp1")
        self.assertEqual(fetched.alert_name, "CheckoutHighErrors")
        self.assertEqual(fetched.service, "checkout")
        self.assertEqual(fetched.status, "investigating")

    def test_get_unknown_fingerprint_returns_none(self):
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_get_by_thread_finds_matching_record(self):
        created = self.store.create("fp1", alert_name="A", service="checkout", severity="critical")

        found = self.store.get_by_thread(created.thread_id)

        self.assertEqual(found.fingerprint, "fp1")

    def test_update_changes_fields_and_bumps_updated_at(self):
        created = self.store.create("fp1", alert_name="A", service="checkout", severity="critical")
        original_updated_at = created.updated_at

        updated = self.store.update("fp1", status="completed", content="root cause: OOMKilled")

        self.assertEqual(updated.status, "completed")
        self.assertEqual(updated.content, "root cause: OOMKilled")
        self.assertGreaterEqual(updated.updated_at, original_updated_at)

    def test_update_unknown_fingerprint_returns_none(self):
        self.assertIsNone(self.store.update("does-not-exist", status="completed"))

    def test_list_returns_newest_first(self):
        self.store.create("fp1", alert_name="A", service="checkout", severity="critical")
        self.store.create("fp2", alert_name="B", service="frontend", severity="warning")
        # Force a distinguishable ordering regardless of clock resolution.
        self.store.update("fp1", created_at="2020-01-01T00:00:00+00:00")
        self.store.update("fp2", created_at="2020-01-02T00:00:00+00:00")

        records = self.store.list()

        self.assertEqual([r.fingerprint for r in records], ["fp2", "fp1"])


class IncidentStoreQueryHistoryTests(TestCase):
    def setUp(self):
        self.store = IncidentStore()
        self.store.create("fp1", alert_name="CheckoutHighErrors", service="checkout", severity="critical")
        self.store.update("fp1", created_at="2026-08-01T00:00:00+00:00")
        self.store.create("fp2", alert_name="FrontendPodDown", service="frontend", severity="warning")
        self.store.update("fp2", created_at="2026-08-02T00:00:00+00:00")
        self.store.create("fp3", alert_name="CheckoutPodDown", service="checkout", severity="critical")
        self.store.update("fp3", created_at="2026-08-03T00:00:00+00:00")

    def test_filters_by_service_substring_case_insensitive(self):
        results = self.store.query_history(service="CHECK", since_hours=24 * 365 * 10)

        self.assertEqual({r.fingerprint for r in results}, {"fp1", "fp3"})

    def test_empty_service_returns_all(self):
        results = self.store.query_history(service="", since_hours=24 * 365 * 10)

        self.assertEqual(len(results), 3)

    def test_results_are_newest_first(self):
        results = self.store.query_history(since_hours=24 * 365 * 10)

        self.assertEqual([r.fingerprint for r in results], ["fp3", "fp2", "fp1"])

    def test_limit_caps_results(self):
        results = self.store.query_history(since_hours=24 * 365 * 10, limit=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fingerprint, "fp3")

    def test_unmatched_service_returns_empty(self):
        results = self.store.query_history(service="payment", since_hours=24 * 365 * 10)

        self.assertEqual(results, [])


class IncidentStorePersistenceTests(TestCase):
    def test_records_survive_reopening_the_same_db_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "incidents.db")

            store1 = IncidentStore(db_path)
            store1.create("fp1", alert_name="CheckoutHighErrors", service="checkout", severity="critical")
            store1.update("fp1", status="resolved", content="fixed by scaling back up")
            store1.close()

            store2 = IncidentStore(db_path)  # simulates a process restart
            record = store2.get("fp1")

            self.assertIsNotNone(record)
            self.assertEqual(record.status, "resolved")
            self.assertEqual(record.content, "fixed by scaling back up")
            store2.close()

    def test_memory_db_does_not_persist_across_instances(self):
        store1 = IncidentStore()  # ":memory:"
        store1.create("fp1", alert_name="A", service="checkout", severity="critical")
        store1.close()

        store2 = IncidentStore()

        self.assertIsNone(store2.get("fp1"))
