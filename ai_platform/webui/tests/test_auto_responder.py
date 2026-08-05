"""
Unit tests for the alert auto-triage module (auto_responder.py).

All mocked / in-memory — no cluster, network, or LLM key needed. The fake
copilot below stands in for SRECopilot's `.ask()`.
"""

import threading
import time
from unittest import TestCase
from unittest.mock import MagicMock

# No sys.path setup needed here: `ai_platform` is a real package (rooted at
# the repo root, which pytest puts on sys.path automatically once it walks
# up the __init__.py chain from this test file) — `import ai_platform.x.y`
# just works.
import ai_platform.webui.auto_responder as auto_responder
from ai_platform.copilot.agent import CopilotReply
from ai_platform.webui.auto_responder import (
    IncidentStore,
    handle_alertmanager_webhook,
    investigate_incident,
    record_approval_outcome,
)


def _alert(alertname="CheckoutHighErrors", status="firing", severity="critical", fingerprint="fp1", **extra_labels):
    labels = {"alertname": alertname, "severity": severity, **extra_labels}
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": "checkout error rate is high"},
        "fingerprint": fingerprint,
        "startsAt": "2026-08-02T00:00:00Z",
    }


class HandleAlertmanagerWebhookTests(TestCase):
    def setUp(self):
        self.store = IncidentStore()

    def test_new_firing_alert_is_registered_and_returned_for_investigation(self):
        payload = {"alerts": [_alert()]}

        new_fps = handle_alertmanager_webhook(payload, self.store)

        self.assertEqual(new_fps, ["fp1"])
        record = self.store.get("fp1")
        self.assertIsNotNone(record)
        self.assertEqual(record.alert_name, "CheckoutHighErrors")
        self.assertEqual(record.severity, "critical")
        self.assertEqual(record.status, "investigating")

    def test_service_resolved_from_explicit_label(self):
        payload = {"alerts": [_alert(service="checkout")]}

        handle_alertmanager_webhook(payload, self.store)

        self.assertEqual(self.store.get("fp1").service, "checkout")

    def test_service_resolved_by_substring_match_on_alert_name(self):
        payload = {"alerts": [_alert(alertname="FrontendPodDown", fingerprint="fp2")]}

        handle_alertmanager_webhook(payload, self.store)

        self.assertEqual(self.store.get("fp2").service, "frontend")

    def test_repeat_notification_of_same_firing_alert_is_not_reinvestigated(self):
        payload = {"alerts": [_alert()]}
        handle_alertmanager_webhook(payload, self.store)

        new_fps = handle_alertmanager_webhook(payload, self.store)

        self.assertEqual(new_fps, [])

    def test_resolved_alert_marks_existing_record_resolved_and_is_not_investigated(self):
        handle_alertmanager_webhook({"alerts": [_alert(status="firing")]}, self.store)

        new_fps = handle_alertmanager_webhook({"alerts": [_alert(status="resolved")]}, self.store)

        self.assertEqual(new_fps, [])
        self.assertEqual(self.store.get("fp1").status, "resolved")

    def test_resolved_alert_with_no_prior_record_is_a_no_op(self):
        new_fps = handle_alertmanager_webhook({"alerts": [_alert(status="resolved", fingerprint="never-seen")]}, self.store)

        self.assertEqual(new_fps, [])
        self.assertIsNone(self.store.get("never-seen"))

    def test_multiple_alerts_in_one_payload_are_all_handled(self):
        payload = {"alerts": [_alert(fingerprint="a"), _alert(fingerprint="b", alertname="PaymentErrors")]}

        new_fps = handle_alertmanager_webhook(payload, self.store)

        self.assertEqual(set(new_fps), {"a", "b"})


class InvestigateIncidentTests(TestCase):
    def setUp(self):
        self.store = IncidentStore()
        handle_alertmanager_webhook({"alerts": [_alert(service="checkout")]}, self.store)

    def test_completed_investigation_updates_record(self):
        copilot = MagicMock()
        copilot.ask.return_value = CopilotReply(content="Root cause: checkout OOMKilled.", steps=[{"type": "call", "name": "investigate_service"}])

        investigate_incident(copilot, self.store, "fp1")

        record = self.store.get("fp1")
        self.assertEqual(record.status, "completed")
        self.assertIn("OOMKilled", record.content)
        copilot.ask.assert_called_once()
        thread_id_arg = copilot.ask.call_args.kwargs["thread_id"]
        self.assertEqual(thread_id_arg, record.thread_id)

    def test_investigation_with_pending_remediation_marks_awaiting_approval(self):
        copilot = MagicMock()
        copilot.ask.return_value = CopilotReply(
            content="",
            pending_action={"action": "scale_deployment", "service_name": "checkout", "desired_replicas": 1, "reason": "scaled to 0"},
        )

        investigate_incident(copilot, self.store, "fp1")

        record = self.store.get("fp1")
        self.assertEqual(record.status, "awaiting_approval")
        self.assertEqual(record.pending_action["action"], "scale_deployment")

    def test_investigation_failure_marks_error_without_raising(self):
        copilot = MagicMock()
        copilot.ask.side_effect = RuntimeError("prometheus unreachable")

        investigate_incident(copilot, self.store, "fp1")  # must not raise

        record = self.store.get("fp1")
        self.assertEqual(record.status, "error")
        self.assertIn("prometheus unreachable", record.error)

    def test_unknown_fingerprint_is_a_no_op(self):
        copilot = MagicMock()

        investigate_incident(copilot, self.store, "does-not-exist")

        copilot.ask.assert_not_called()


class InvestigationConcurrencyLimitTests(TestCase):
    """
    Regression tests for the finding that background investigations had no
    concurrency cap — a 40-alert cascade would fire 40 concurrent
    multi-LLM-call investigations. Temporarily swaps in a capacity-1
    semaphore so the bound is exercisable without waiting on the real
    default (3).
    """

    def setUp(self):
        self.store = IncidentStore()
        handle_alertmanager_webhook({"alerts": [_alert(service="checkout", fingerprint="fp1")]}, self.store)
        handle_alertmanager_webhook(
            {"alerts": [_alert(service="cart", fingerprint="fp2", alertname="CartErrors")]}, self.store
        )
        self._original_semaphore = auto_responder._investigation_semaphore
        auto_responder._investigation_semaphore = threading.Semaphore(1)

    def tearDown(self):
        auto_responder._investigation_semaphore = self._original_semaphore

    def test_second_investigation_is_queued_while_the_only_slot_is_held(self):
        first_started = threading.Event()
        release_first = threading.Event()

        def slow_ask(*args, **kwargs):
            first_started.set()
            release_first.wait(timeout=5)
            return CopilotReply(content="done")

        copilot = MagicMock()
        copilot.ask.side_effect = slow_ask

        t1 = threading.Thread(target=investigate_incident, args=(copilot, self.store, "fp1"))
        t1.start()
        self.assertTrue(first_started.wait(timeout=2), "first investigation never started")

        t2 = threading.Thread(target=investigate_incident, args=(copilot, self.store, "fp2"))
        t2.start()
        time.sleep(0.2)  # give t2 a moment to hit the semaphore and mark itself queued
        self.assertEqual(self.store.get("fp2").status, "queued")

        release_first.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(self.store.get("fp1").status, "completed")
        self.assertEqual(self.store.get("fp2").status, "completed")

    def test_slot_is_released_after_a_failed_investigation_not_leaked(self):
        copilot = MagicMock()
        copilot.ask.side_effect = RuntimeError("boom")
        investigate_incident(copilot, self.store, "fp1")  # fails, must still release its slot

        # If the slot leaked, this would block forever — bounded with a
        # thread + join timeout so a regression fails the test rather than
        # hanging the whole suite.
        copilot2 = MagicMock()
        copilot2.ask.return_value = CopilotReply(content="done")
        t = threading.Thread(target=investigate_incident, args=(copilot2, self.store, "fp2"))
        t.start()
        t.join(timeout=2)

        self.assertFalse(t.is_alive(), "second investigation never got a slot — the first one's slot leaked")
        self.assertEqual(self.store.get("fp2").status, "completed")


class RecordApprovalOutcomeTests(TestCase):
    def setUp(self):
        self.store = IncidentStore()
        handle_alertmanager_webhook({"alerts": [_alert(service="checkout")]}, self.store)
        self.thread_id = self.store.get("fp1").thread_id

    def test_approval_outcome_marks_completed(self):
        reply = CopilotReply(content="Done — scaled checkout back to 1 replica.")

        record_approval_outcome(self.store, self.thread_id, reply)

        record = self.store.get("fp1")
        self.assertEqual(record.status, "completed")
        self.assertIn("scaled checkout", record.content)

    def test_unknown_thread_id_is_a_no_op(self):
        reply = CopilotReply(content="whatever")

        record_approval_outcome(self.store, "no-such-thread", reply)  # must not raise

        self.assertEqual(self.store.get("fp1").status, "investigating")
