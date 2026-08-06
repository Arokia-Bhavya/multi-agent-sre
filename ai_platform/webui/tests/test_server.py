"""
Unit tests for the Milestone 6 copilot web UI's FastAPI backend.

No cluster, network, API key, or live server is required: `server._copilot`
is monkeypatched directly to a fake object with `.ask()`/`.respond_to_approval()`,
which bypasses `build_copilot()` (the only piece that would need a real
cluster/LLM key) entirely.
"""

import os
import sys
from unittest.mock import MagicMock


from unittest import TestCase

from fastapi.testclient import TestClient

import ai_platform.webui.server as server
from ai_platform.copilot.agent import CopilotReply
from ai_platform.webui.auto_responder import IncidentStore


class WebUIServerTests(TestCase):
    def setUp(self):
        self.fake_copilot = MagicMock()
        server._copilot = self.fake_copilot  # bypass build_copilot()
        server._incidents = IncidentStore()  # isolate auto-triage state per test
        self.client = TestClient(server.app)

    def tearDown(self):
        server._copilot = None

    def test_new_session_returns_a_thread_id(self):
        res = self.client.post("/api/session")
        self.assertEqual(res.status_code, 200)
        thread_id = res.json()["thread_id"]
        self.assertTrue(thread_id)

        res2 = self.client.post("/api/session")
        self.assertNotEqual(thread_id, res2.json()["thread_id"])

    def test_chat_returns_content_and_steps(self):
        self.fake_copilot.ask.return_value = CopilotReply(
            content="One active alert: CheckoutHighErrors.",
            steps=[{"type": "call", "name": "get_active_alerts", "args": {}}],
        )

        res = self.client.post(
            "/api/chat", json={"thread_id": "t1", "message": "Show me the latest alerts"}
        )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["content"], "One active alert: CheckoutHighErrors.")
        self.assertIsNone(body["pending_action"])
        self.assertEqual(body["steps"][0]["name"], "get_active_alerts")
        self.fake_copilot.ask.assert_called_once_with("Show me the latest alerts", thread_id="t1")

    def test_chat_returns_pending_action_when_remediation_proposed(self):
        self.fake_copilot.ask.return_value = CopilotReply(
            content="",
            pending_action={
                "action": "scale_deployment",
                "service_name": "checkout",
                "desired_replicas": 1,
                "reason": "checkout was scaled to 0",
            },
        )

        res = self.client.post(
            "/api/chat", json={"thread_id": "t1", "message": "Fix checkout"}
        )

        body = res.json()
        self.assertEqual(body["pending_action"]["action"], "scale_deployment")
        self.assertEqual(body["pending_action"]["desired_replicas"], 1)

    def test_approve_forwards_boolean_decision(self):
        self.fake_copilot.respond_to_approval.return_value = CopilotReply(
            content="Done — checkout is back to 1 replica."
        )

        res = self.client.post("/api/approve", json={"thread_id": "t1", "approved": True})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["content"], "Done — checkout is back to 1 replica.")
        self.fake_copilot.respond_to_approval.assert_called_once_with(True, thread_id="t1")

    def test_reject_forwards_false(self):
        self.fake_copilot.respond_to_approval.return_value = CopilotReply(
            content="Okay, I did not make any changes."
        )

        res = self.client.post("/api/approve", json={"thread_id": "t1", "approved": False})

        self.fake_copilot.respond_to_approval.assert_called_once_with(False, thread_id="t1")
        self.assertEqual(res.json()["content"], "Okay, I did not make any changes.")

    def test_chat_error_returns_500_with_detail(self):
        self.fake_copilot.ask.side_effect = RuntimeError("prometheus unreachable")

        res = self.client.post("/api/chat", json={"thread_id": "t1", "message": "hello"})

        self.assertEqual(res.status_code, 500)
        self.assertIn("prometheus unreachable", res.json()["detail"])

    def test_index_serves_html(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("AI SRE Copilot", res.text)

    def test_alerts_returns_simplified_rule_list(self):
        self.fake_copilot.alert_agent.get_all_alert_rules.return_value = [
            {
                "name": "ProductCatalogPodDown",
                "state": "firing",
                "labels": {"severity": "critical", "service": "product-catalog"},
                "annotations": {"summary": "product-catalog pod is down", "description": "..."},
                "alerts": [{"activeAt": "2026-07-29T16:12:51.312502685Z"}],
            },
            {
                "name": "FrontendHighLatency",
                "state": "inactive",
                "labels": {"severity": "warning", "service": "frontend"},
                "annotations": {"summary": "Frontend p95 latency is above 3 seconds"},
                "alerts": [],
            },
        ]

        res = self.client.get("/api/alerts")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["name"], "ProductCatalogPodDown")
        self.assertEqual(body[0]["state"], "firing")
        self.assertEqual(body[0]["service"], "product-catalog")
        self.assertEqual(body[0]["active_since"], "2026-07-29T16:12:51.312502685Z")
        self.assertEqual(body[1]["state"], "inactive")
        self.assertIsNone(body[1]["active_since"])

    def test_alerts_defaults_missing_labels_to_unknown(self):
        self.fake_copilot.alert_agent.get_all_alert_rules.return_value = [
            {"name": "SomeRule", "state": "inactive", "labels": {}, "annotations": {}, "alerts": []},
        ]

        res = self.client.get("/api/alerts")

        body = res.json()
        self.assertEqual(body[0]["severity"], "unknown")
        self.assertEqual(body[0]["service"], "unknown")

    def test_alerts_error_returns_500(self):
        self.fake_copilot.alert_agent.get_all_alert_rules.side_effect = RuntimeError("prometheus unreachable")

        res = self.client.get("/api/alerts")

        self.assertEqual(res.status_code, 500)
        self.assertIn("prometheus unreachable", res.json()["detail"])

    def test_incidents_starts_empty(self):
        res = self.client.get("/api/incidents")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_webhook_registers_and_investigates_new_firing_alert(self):
        self.fake_copilot.ask.return_value = CopilotReply(content="Root cause: OOMKilled.")
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "CheckoutHighErrors", "severity": "critical", "service": "checkout"},
                    "annotations": {"summary": "error rate high"},
                    "fingerprint": "fp1",
                    "startsAt": "2026-08-02T00:00:00Z",
                }
            ]
        }

        res = self.client.post("/api/webhook/alertmanager", json=payload)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["investigating"], ["fp1"])
        self.fake_copilot.ask.assert_called_once()

        incidents_res = self.client.get("/api/incidents")
        body = incidents_res.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["alert_name"], "CheckoutHighErrors")
        self.assertEqual(body[0]["status"], "completed")
        self.assertIn("OOMKilled", body[0]["content"])

    def test_webhook_ignores_repeat_notification_of_same_firing_alert(self):
        self.fake_copilot.ask.return_value = CopilotReply(content="Root cause: OOMKilled.")
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "CheckoutHighErrors", "severity": "critical"},
                    "annotations": {},
                    "fingerprint": "fp1",
                    "startsAt": "2026-08-02T00:00:00Z",
                }
            ]
        }

        self.client.post("/api/webhook/alertmanager", json=payload)
        res2 = self.client.post("/api/webhook/alertmanager", json=payload)

        self.assertEqual(res2.json()["investigating"], [])
        self.fake_copilot.ask.assert_called_once()

    def test_approve_updates_matching_incident_record(self):
        self.fake_copilot.ask.return_value = CopilotReply(
            content="",
            pending_action={"action": "scale_deployment", "service_name": "checkout", "desired_replicas": 1, "reason": "scaled to 0"},
        )
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "CheckoutHighErrors", "severity": "critical", "service": "checkout"},
                    "annotations": {},
                    "fingerprint": "fp1",
                    "startsAt": "2026-08-02T00:00:00Z",
                }
            ]
        }
        self.client.post("/api/webhook/alertmanager", json=payload)
        thread_id = self.client.get("/api/incidents").json()[0]["thread_id"]

        self.fake_copilot.respond_to_approval.return_value = CopilotReply(content="Done — scaled back to 1 replica.")
        approve_res = self.client.post("/api/approve", json={"thread_id": thread_id, "approved": True})

        self.assertEqual(approve_res.status_code, 200)
        incident = self.client.get("/api/incidents").json()[0]
        self.assertEqual(incident["status"], "completed")
        self.assertIn("scaled back", incident["content"])
