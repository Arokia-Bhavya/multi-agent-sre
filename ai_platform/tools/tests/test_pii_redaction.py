"""Unit tests for the PII/secret redaction utility."""

from unittest import TestCase

from ai_platform.tools.pii_redaction import redact_text, redact_structure


class RedactTextTests(TestCase):
    def test_redacts_email_address(self):
        result = redact_text("user contact: jane.doe@example.com failed checkout")
        self.assertNotIn("jane.doe@example.com", result)
        self.assertIn("[REDACTED_EMAIL]", result)

    def test_redacts_ssn(self):
        result = redact_text("customer ssn 123-45-6789 on file")
        self.assertNotIn("123-45-6789", result)
        self.assertIn("[REDACTED_SSN]", result)

    def test_redacts_16_digit_card_number(self):
        result = redact_text("card 4111111111111111 declined")
        self.assertNotIn("4111111111111111", result)
        self.assertIn("[REDACTED_CARD_NUMBER]", result)

    def test_redacts_grouped_card_number(self):
        result = redact_text("card 4111-1111-1111-1111 declined")
        self.assertNotIn("4111-1111-1111-1111", result)
        self.assertIn("[REDACTED_CARD_NUMBER]", result)

    def test_redacts_phone_number(self):
        result = redact_text("call customer at 555-123-4567 about the order")
        self.assertNotIn("555-123-4567", result)
        self.assertIn("[REDACTED_PHONE]", result)

    def test_redacts_bearer_token(self):
        result = redact_text("Authorization: Bearer sk-abcdefghijklmnopqrstuvwx")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", result)
        self.assertIn("[REDACTED_SECRET]", result)

    def test_does_not_redact_13_digit_epoch_millis_timestamp(self):
        # Regression: a naive 13-19 digit "card number" pattern would eat
        # every Unix-epoch-ms timestamp in a log line, which is exactly the
        # kind of thing an SRE actually needs to see.
        result = redact_text("request started at 1735689600123 duration=45ms")
        self.assertIn("1735689600123", result)

    def test_does_not_redact_ip_address(self):
        # IPs are operational signal (pod/service IPs), not PII on their own.
        result = redact_text("connection refused from 10.244.1.23:8080")
        self.assertIn("10.244.1.23", result)

    def test_leaves_ordinary_log_text_unchanged(self):
        text = "INFO checkout-abc123: order placed, latency=120ms status=200"
        self.assertEqual(redact_text(text), text)

    def test_non_string_input_is_returned_unchanged(self):
        self.assertEqual(redact_text(None), None)
        self.assertEqual(redact_text(""), "")


class RedactStructureTests(TestCase):
    def test_redacts_strings_nested_in_dicts_and_lists(self):
        data = {
            "events": [
                {"message": "failed to notify jane.doe@example.com", "count": 3},
                {"message": "OOMKilled", "count": 1},
            ],
            "duration_ms": 42.5,
            "healthy": False,
        }
        result = redact_structure(data)
        self.assertNotIn("jane.doe@example.com", result["events"][0]["message"])
        self.assertEqual(result["events"][1]["message"], "OOMKilled")
        self.assertEqual(result["duration_ms"], 42.5)
        self.assertEqual(result["healthy"], False)

    def test_non_container_values_pass_through(self):
        self.assertEqual(redact_structure(42), 42)
        self.assertEqual(redact_structure(None), None)
        self.assertEqual(redact_structure(True), True)
