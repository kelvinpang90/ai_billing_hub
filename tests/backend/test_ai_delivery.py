"""Pure delivery rules pinned by AI module design gate #281 v23."""

from __future__ import annotations

from app.ai_api.delivery import _outcome, _retry_delay


def test_a_mismatched_success_receipt_is_retryable() -> None:
    delivered, retryable, error = _outcome(
        202,
        b'{"data":{"event_id":"other","status":"accepted"}}',
        "event",
    )

    assert (delivered, retryable, error) == (False, True, "INVALID_BILLING_RECEIPT")


def test_non_envelope_5xx_retries_but_other_non_envelopes_die() -> None:
    assert _outcome(503, b"not json", "event") == (False, True, "BILLING_UNAVAILABLE")
    assert _outcome(503, b'{"error":{"code":"IGNORED"}}', "event") == (
        False,
        True,
        "BILLING_UNAVAILABLE",
    )
    assert _outcome(422, b"not json", "event") == (False, False, "INVALID_BILLING_RESPONSE")


def test_backoff_includes_the_configured_jitter_bounds() -> None:
    assert _retry_delay(1, lambda _low, _high: 0.5) == 1
    assert _retry_delay(12, lambda _low, _high: 1.0) == 3600
    assert _retry_delay(20, lambda _low, _high: 0.5) == 1800
