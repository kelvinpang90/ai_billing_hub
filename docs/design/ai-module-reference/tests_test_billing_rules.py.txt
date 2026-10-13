"""Billing contract rules, standard library only.

Runs in the OpenClaw Worker sandbox (commands.yaml: tests.rules) as well as in CI. Do not import
httpx, FastAPI or SQLAlchemy here: the sandbox has no ssl and no loopback.
"""

import hashlib
import hmac
import unittest

from app.billing_rules import canonical_request, error_outcome, receipt_acknowledges, sign, status_allows_ai

EVENT_ID = "01J00000000000000000000000"


class SignatureTests(unittest.TestCase):
    def test_canonical_request_is_five_lf_joined_lines_without_trailing_newline(self):
        # ai_billing_hub docs/api.md: method, path, timestamp, request id, SHA-256 of the raw body.
        body = b'{"event_id":"x"}'
        self.assertEqual(
            canonical_request("POST", "/api/v1/integration/usage-events", "1700000000", "attempt-1", body),
            "POST\n/api/v1/integration/usage-events\n1700000000\nattempt-1\n" + hashlib.sha256(body).hexdigest(),
        )

    def test_empty_body_hashes_the_empty_string(self):
        # The status query is a GET with an empty body; the hash still occupies the fifth line.
        line = canonical_request("GET", "/p", "1", "a", b"").split("\n")[4]
        self.assertEqual(line, hashlib.sha256(b"").hexdigest())

    def test_signature_is_lowercase_hex_hmac_sha256_of_the_canonical_request(self):
        canonical = canonical_request("POST", "/p", "1", "a", b"{}")
        signature = sign("sk_test_only", canonical)
        self.assertEqual(signature, hmac.new(b"sk_test_only", canonical.encode(), hashlib.sha256).hexdigest())
        self.assertRegex(signature, r"^[0-9a-f]{64}$")

    def test_a_retry_with_a_new_attempt_id_changes_the_signature(self):
        # Billing Hub treats a reused request id as a replay, so every attempt is signed afresh.
        first = sign("s", canonical_request("POST", "/p", "1", "attempt-1", b"{}"))
        second = sign("s", canonical_request("POST", "/p", "1", "attempt-2", b"{}"))
        self.assertNotEqual(first, second)


class ReceiptTests(unittest.TestCase):
    def receipt(self, event_id=EVENT_ID, status="accepted"):
        return {"data": {"event_id": event_id, "status": status}}

    def test_accepted_and_duplicate_receipts_for_this_event_acknowledge(self):
        self.assertTrue(receipt_acknowledges(202, self.receipt(), EVENT_ID))
        self.assertTrue(receipt_acknowledges(200, self.receipt(status="already_received"), EVENT_ID))
        self.assertTrue(receipt_acknowledges(200, self.receipt(status="already_processed"), EVENT_ID))

    def test_a_receipt_for_another_event_does_not_acknowledge(self):
        # Otherwise a misrouted receipt would mark an undelivered event as billed.
        self.assertFalse(receipt_acknowledges(202, self.receipt(event_id="01JOTHER"), EVENT_ID))

    def test_status_must_match_the_http_code(self):
        self.assertFalse(receipt_acknowledges(200, self.receipt(status="accepted"), EVENT_ID))
        self.assertFalse(receipt_acknowledges(202, self.receipt(status="already_processed"), EVENT_ID))

    def test_malformed_receipts_do_not_acknowledge(self):
        for document in (None, [], {}, {"data": None}, {"data": {"event_id": EVENT_ID}}, self.receipt(status=["accepted"])):
            with self.subTest(document=document):
                self.assertFalse(receipt_acknowledges(202, document, EVENT_ID))


class RetryTests(unittest.TestCase):
    def test_only_an_explicit_true_retryable_authorizes_a_retry(self):
        # docs/api.md: integrators decide from retryable alone, never from the HTTP status.
        self.assertEqual(error_outcome({"error": {"code": "SERVICE_UNAVAILABLE"}, "retryable": True}), ("SERVICE_UNAVAILABLE", True))
        self.assertEqual(error_outcome({"error": {"code": "IDEMPOTENCY_CONFLICT"}, "retryable": False}), ("IDEMPOTENCY_CONFLICT", False))
        for retryable in (None, "true", 1):
            with self.subTest(retryable=retryable):
                self.assertFalse(error_outcome({"error": {"code": "X"}, "retryable": retryable})[1])

    def test_missing_retryable_goes_dead_rather_than_retrying_forever(self):
        self.assertEqual(error_outcome({"error": {"code": "X"}}), ("X", False))

    def test_unreadable_error_bodies_are_not_retried(self):
        self.assertEqual(error_outcome(None), ("INVALID_BILLING_RESPONSE", False))
        self.assertEqual(error_outcome(["x"]), ("INVALID_BILLING_RESPONSE", False))
        self.assertEqual(error_outcome({"error": "x"}), ("INVALID_BILLING_RESPONSE", False))

    def test_error_code_is_a_bounded_string(self):
        self.assertEqual(error_outcome({"error": {"code": 7}, "retryable": True}), ("BILLING_ERROR", True))
        self.assertEqual(len(error_outcome({"error": {"code": "E" * 100}})[0]), 64)


class StatusTests(unittest.TestCase):
    def status(self, **overrides):
        data = {"tenant_id": "t1", "project_id": "p1", "effective_status": "ALLOW_AI", "status_version": 0}
        data.update(overrides)
        return data

    def test_verified_status_decides(self):
        self.assertTrue(status_allows_ai(self.status(), "t1", "p1"))
        self.assertFalse(status_allows_ai(self.status(effective_status="BLOCK_AI", status_version=3), "t1", "p1"))

    def test_another_tenants_or_projects_status_is_unverifiable(self):
        # The caller fails open on ValueError, so a foreign BLOCK_AI cannot stop this tenant.
        for data in (self.status(tenant_id="t2"), self.status(project_id="p2")):
            with self.subTest(data=data), self.assertRaises(ValueError):
                status_allows_ai(data, "t1", "p1")

    def test_status_version_must_be_a_strict_integer(self):
        for version in (True, False, 1.0, "1", None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                status_allows_ai(self.status(effective_status="BLOCK_AI", status_version=version), "t1", "p1")

    def test_unknown_or_malformed_status_is_unverifiable(self):
        for data in (self.status(effective_status="MAYBE"), self.status(effective_status=["BLOCK_AI"]), {}, None, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                status_allows_ai(data, "t1", "p1")


if __name__ == "__main__":
    unittest.main()
