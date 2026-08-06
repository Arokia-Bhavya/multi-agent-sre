"""
Unit tests for the Milestone 6 copilot web UI's FastAPI backend.

No cluster, network, or real Google account is required: `server._copilot`
is monkeypatched directly to a fake object with `.ask()`/`.respond_to_approval()`,
which bypasses `build_copilot()` (the only piece that would need a real
cluster/LLM key) entirely. Google sign-in is simulated by constructing the
same signed session cookie `starlette.middleware.sessions.SessionMiddleware`
itself would set after a real `/auth/callback` — see `_signed_session_cookie`
below — rather than driving a real OAuth redirect through Google.
"""

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from unittest import TestCase

import itsdangerous
from fastapi.testclient import TestClient

import ai_platform.webui.server as server
from ai_platform.copilot.agent import CopilotReply
from ai_platform.webui.auto_responder import IncidentStore


def _signed_session_cookie(secret_key: str, session_dict: dict) -> str:
    """
    Builds a cookie value byte-for-byte compatible with what
    `starlette.middleware.sessions.SessionMiddleware` sets after a real
    sign-in: `itsdangerous.TimestampSigner(secret_key).sign(b64encode(json))`.
    Lets tests simulate "already signed in" without driving a real Google
    OAuth redirect.
    """
    signer = itsdangerous.TimestampSigner(str(secret_key))
    data = base64.b64encode(json.dumps(session_dict).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


class WebUIServerTests(TestCase):
    def setUp(self):
        self.fake_copilot = MagicMock()
        server._copilot = self.fake_copilot  # bypass build_copilot()
        server._incidents = IncidentStore()  # isolate auto-triage state per test
        # The Alertmanager webhook is the only route still gated by the
        # shared-secret model; default it to the explicit opt-out ("") so
        # tests unrelated to that endpoint's auth aren't affected by it.
        server.WEBUI_API_KEY = ""
        self.client = TestClient(server.app)
        self._login()

    def tearDown(self):
        server._copilot = None
        server.WEBUI_API_KEY = ""  # reset in case a test enabled it

    def _login(self, email="test@example.com", name="Test User"):
        """Attach a valid, signed-in session cookie to self.client."""
        cookie = _signed_session_cookie(
            server.SESSION_SECRET_KEY,
            {"user": {"email": email, "name": name, "picture": None}},
        )
        self.client.cookies.set("session", cookie)

    def _logout(self):
        self.client.cookies.delete("session")

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

    def test_healthz_returns_ok_without_auth(self):
        self._logout()
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_index_serves_html(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("AI SRE Copilot", res.text)

    def test_index_redirects_to_login_when_signed_out_and_oidc_configured(self):
        self._logout()
        with patch.object(server, "OIDC_CONFIGURED", True):
            res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 307)
        self.assertTrue(res.headers["location"].endswith("/login"))

    def test_login_page_serves_html_without_auth(self):
        self._logout()
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Sign in", res.text)

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

    def test_webhook_open_when_key_explicitly_disabled(self):
        # server.WEBUI_API_KEY = "" (set in setUp) simulates the explicit
        # opt-out (`WEBUI_API_KEY=` empty in .env) for the webhook route —
        # the one way to get its fully-open behavior back on purpose.
        res = self.client.post("/api/webhook/alertmanager", json={"alerts": []})
        self.assertEqual(res.status_code, 200)

    def test_webhook_rejects_missing_key_when_configured(self):
        server.WEBUI_API_KEY = "test-secret-key"

        res = self.client.post("/api/webhook/alertmanager", json={"alerts": []})

        self.assertEqual(res.status_code, 401)

    def test_webhook_rejects_wrong_key_when_configured(self):
        server.WEBUI_API_KEY = "test-secret-key"

        res = self.client.post(
            "/api/webhook/alertmanager", json={"alerts": []}, headers={"X-API-Key": "wrong-key"}
        )

        self.assertEqual(res.status_code, 401)

    def test_webhook_accepts_correct_x_api_key_header(self):
        server.WEBUI_API_KEY = "test-secret-key"

        res = self.client.post(
            "/api/webhook/alertmanager", json={"alerts": []}, headers={"X-API-Key": "test-secret-key"}
        )

        self.assertEqual(res.status_code, 200)

    def test_webhook_accepts_bearer_token(self):
        server.WEBUI_API_KEY = "test-secret-key"

        res = self.client.post(
            "/api/webhook/alertmanager",
            json={"alerts": []},
            headers={"Authorization": "Bearer test-secret-key"},
        )

        self.assertEqual(res.status_code, 200)

    def test_index_and_static_and_login_stay_open_regardless_of_session(self):
        self._logout()
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        # "/" only redirects when OIDC is actually configured (see the
        # dedicated test above); with it unconfigured (the default in these
        # tests), "/" still serves the shell so local dev without Google
        # credentials configured isn't completely locked out of seeing it.
        res2 = self.client.get("/")
        self.assertEqual(res2.status_code, 200)

    def test_api_routes_reject_requests_without_a_session(self):
        self._logout()

        res = self.client.post("/api/session")

        self.assertEqual(res.status_code, 401)

    def test_api_routes_accept_requests_with_a_valid_session(self):
        res = self.client.post("/api/session")

        self.assertEqual(res.status_code, 200)

    def test_me_returns_the_signed_in_user(self):
        res = self.client.get("/api/me")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["email"], "test@example.com")

    def test_me_returns_401_when_signed_out(self):
        self._logout()

        res = self.client.get("/api/me")

        self.assertEqual(res.status_code, 401)

    def test_auth_logout_clears_session(self):
        res = self.client.get("/auth/logout", follow_redirects=False)

        self.assertEqual(res.status_code, 307)
        self.assertTrue(res.headers["location"].endswith("/login"))
        # SessionMiddleware expires the cookie by sending a `session=null`
        # Set-Cookie with an epoch expiry — the real signal a browser (not
        # necessarily httpx's test cookie jar) uses to drop it. Checking the
        # header directly is more robust than round-tripping through the
        # test client's own cookie jar, whose deletion semantics differ.
        self.assertIn("session=null", res.headers["set-cookie"])
        self.assertIn("1970", res.headers["set-cookie"])

        self._logout()  # simulate the browser having dropped the cookie
        res2 = self.client.get("/api/me")
        self.assertEqual(res2.status_code, 401)

    def test_auth_login_503_when_oidc_not_configured(self):
        with patch.object(server, "OIDC_CONFIGURED", False):
            res = self.client.get("/auth/login")
        self.assertEqual(res.status_code, 503)

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


class OperationsHubTests(TestCase):
    """
    Unit tests for GET /api/operations-hub — the KPI aggregation behind the
    web UI's Operations Hub tab. Seeds `IncidentStore` records directly with
    controlled `created_at`/`updated_at` timestamps (rather than going
    through the webhook, which always stamps "now") so the date-bucketing
    and median-duration math can be asserted precisely.
    """

    def setUp(self):
        server._copilot = MagicMock()
        server._incidents = IncidentStore()
        server.WEBUI_API_KEY = ""
        self.client = TestClient(server.app)
        cookie = _signed_session_cookie(
            server.SESSION_SECRET_KEY,
            {"user": {"email": "test@example.com", "name": "Test User", "picture": None}},
        )
        self.client.cookies.set("session", cookie)

    def tearDown(self):
        server._copilot = None
        server.WEBUI_API_KEY = ""

    def _seed(self, fingerprint, status, created_at, updated_at, service="checkout", severity="critical"):
        record = server._incidents.create(fingerprint, "TestAlert", service, severity)
        record.status = status
        record.created_at = created_at
        record.updated_at = updated_at
        return record

    def test_requires_auth(self):
        self.client.cookies.delete("session")

        res = self.client.get("/api/operations-hub")

        self.assertEqual(res.status_code, 401)

    def test_empty_store_returns_zeroed_kpis(self):
        res = self.client.get("/api/operations-hub")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total_incidents"], 0)
        self.assertEqual(body["resolution_rate"], 0.0)
        self.assertEqual(body["pending_approvals"], 0)
        self.assertIsNone(body["median_time_to_mitigate_seconds"])
        self.assertEqual(len(body["daily"]), 7)  # default range_days
        self.assertEqual(body["recent"], [])

    def test_resolution_rate_and_pending_approvals(self):
        now = datetime.now(timezone.utc)
        self._seed("fp1", "completed", (now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=1)).isoformat())
        self._seed("fp2", "resolved", (now - timedelta(hours=3)).isoformat(), (now - timedelta(hours=2)).isoformat())
        self._seed("fp3", "awaiting_approval", (now - timedelta(hours=1)).isoformat(), (now - timedelta(hours=1)).isoformat())
        self._seed("fp4", "investigating", (now - timedelta(minutes=5)).isoformat(), (now - timedelta(minutes=5)).isoformat())

        res = self.client.get("/api/operations-hub")

        body = res.json()
        self.assertEqual(body["total_incidents"], 4)
        self.assertEqual(body["resolved_count"], 2)
        self.assertEqual(body["resolution_rate"], 0.5)
        self.assertEqual(body["pending_approvals"], 1)

    def test_median_time_to_mitigate(self):
        now = datetime.now(timezone.utc)
        # Two resolved incidents: 10 minutes and 30 minutes to mitigate.
        self._seed("fp1", "completed", (now - timedelta(minutes=40)).isoformat(), (now - timedelta(minutes=30)).isoformat())
        self._seed("fp2", "completed", (now - timedelta(minutes=60)).isoformat(), (now - timedelta(minutes=30)).isoformat())

        res = self.client.get("/api/operations-hub")

        body = res.json()
        median_minutes = body["median_time_to_mitigate_seconds"] / 60
        self.assertAlmostEqual(median_minutes, 20, delta=0.1)

    def test_incidents_outside_range_are_excluded(self):
        now = datetime.now(timezone.utc)
        self._seed("fp_old", "completed", (now - timedelta(days=10)).isoformat(), (now - timedelta(days=10)).isoformat())
        self._seed("fp_recent", "completed", (now - timedelta(hours=1)).isoformat(), (now - timedelta(minutes=30)).isoformat())

        res = self.client.get("/api/operations-hub?range_days=7")

        body = res.json()
        self.assertEqual(body["total_incidents"], 1)

    def test_range_days_query_param_changes_bucket_count(self):
        res = self.client.get("/api/operations-hub?range_days=30")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["daily"]), 30)

    def test_recent_list_capped_at_five_newest_first(self):
        now = datetime.now(timezone.utc)
        for i in range(7):
            self._seed(f"fp{i}", "completed", (now - timedelta(hours=i)).isoformat(), (now - timedelta(hours=i)).isoformat())

        res = self.client.get("/api/operations-hub")

        recent = res.json()["recent"]
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0]["fingerprint"], "fp0")  # newest first, per IncidentStore.list()


class ResolveApiKeyTests(TestCase):
    """
    Unit tests for `_resolve_api_key`'s three-way env-var handling: unset
    (auto-generate), explicitly empty (opt out), and explicitly set (use it).
    Isolated from `os.environ` directly rather than going through
    `server.WEBUI_API_KEY` (module-level, already resolved at import time).
    """

    def test_generates_a_key_when_env_var_is_unset(self):
        previous = os.environ.pop("WEBUI_API_KEY", None)
        try:
            key, auto_generated = server._resolve_api_key()
        finally:
            if previous is not None:
                os.environ["WEBUI_API_KEY"] = previous
        self.assertTrue(auto_generated)
        self.assertTrue(key)  # non-empty, unpredictable
        self.assertGreater(len(key), 20)

    def test_two_generated_keys_are_different(self):
        os.environ.pop("WEBUI_API_KEY", None)
        key1, _ = server._resolve_api_key()
        key2, _ = server._resolve_api_key()
        self.assertNotEqual(key1, key2)

    def test_respects_explicitly_empty_env_var_as_opt_out(self):
        os.environ["WEBUI_API_KEY"] = ""
        try:
            key, auto_generated = server._resolve_api_key()
        finally:
            del os.environ["WEBUI_API_KEY"]
        self.assertEqual(key, "")
        self.assertFalse(auto_generated)

    def test_respects_explicitly_configured_key(self):
        os.environ["WEBUI_API_KEY"] = "my-configured-key"
        try:
            key, auto_generated = server._resolve_api_key()
        finally:
            del os.environ["WEBUI_API_KEY"]
        self.assertEqual(key, "my-configured-key")
        self.assertFalse(auto_generated)


class ResolveSessionSecretTests(TestCase):
    """Same three-way handling as WEBUI_API_KEY, but for SESSION_SECRET_KEY."""

    def test_generates_a_secret_when_env_var_is_unset(self):
        previous = os.environ.pop("SESSION_SECRET_KEY", None)
        try:
            key, auto_generated = server._resolve_session_secret()
        finally:
            if previous is not None:
                os.environ["SESSION_SECRET_KEY"] = previous
        self.assertTrue(auto_generated)
        self.assertGreater(len(key), 20)

    def test_respects_explicitly_configured_secret(self):
        os.environ["SESSION_SECRET_KEY"] = "my-configured-secret"
        try:
            key, auto_generated = server._resolve_session_secret()
        finally:
            del os.environ["SESSION_SECRET_KEY"]
        self.assertEqual(key, "my-configured-secret")
        self.assertFalse(auto_generated)


class UserAllowedTests(TestCase):
    """`_user_allowed`'s authorization layer on top of Google's authentication."""

    def setUp(self):
        self._orig_emails = server.ALLOWED_GOOGLE_EMAILS
        self._orig_domain = server.ALLOWED_GOOGLE_DOMAIN

    def tearDown(self):
        server.ALLOWED_GOOGLE_EMAILS = self._orig_emails
        server.ALLOWED_GOOGLE_DOMAIN = self._orig_domain

    def test_allows_any_verified_email_when_no_allowlist_configured(self):
        server.ALLOWED_GOOGLE_EMAILS = set()
        server.ALLOWED_GOOGLE_DOMAIN = ""
        self.assertTrue(server._user_allowed({"email": "anyone@gmail.com", "email_verified": True}))

    def test_rejects_unverified_email(self):
        server.ALLOWED_GOOGLE_EMAILS = set()
        server.ALLOWED_GOOGLE_DOMAIN = ""
        self.assertFalse(server._user_allowed({"email": "anyone@gmail.com", "email_verified": False}))

    def test_rejects_email_not_on_allowlist(self):
        server.ALLOWED_GOOGLE_EMAILS = {"alice@example.com"}
        server.ALLOWED_GOOGLE_DOMAIN = ""
        self.assertFalse(server._user_allowed({"email": "mallory@example.com", "email_verified": True}))

    def test_allows_email_on_allowlist(self):
        server.ALLOWED_GOOGLE_EMAILS = {"alice@example.com"}
        server.ALLOWED_GOOGLE_DOMAIN = ""
        self.assertTrue(server._user_allowed({"email": "alice@example.com", "email_verified": True}))

    def test_rejects_email_outside_allowed_domain(self):
        server.ALLOWED_GOOGLE_EMAILS = set()
        server.ALLOWED_GOOGLE_DOMAIN = "example.com"
        self.assertFalse(server._user_allowed({"email": "someone@other.com", "email_verified": True}))

    def test_allows_email_inside_allowed_domain(self):
        server.ALLOWED_GOOGLE_EMAILS = set()
        server.ALLOWED_GOOGLE_DOMAIN = "example.com"
        self.assertTrue(server._user_allowed({"email": "someone@example.com", "email_verified": True}))
