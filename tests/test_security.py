import pytest

from lead_router.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    compute_signature,
    sign_headers,
    verify_signature,
)

SECRET = "test-secret"
BODY = b'{"name": "Test"}'
NOW = 1_800_000_000


def test_valid_signature_passes():
    headers = sign_headers(SECRET, BODY, now=NOW)
    verify_signature(
        SECRET, BODY, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], 300, now=NOW + 10
    )


def test_tampered_body_is_rejected():
    headers = sign_headers(SECRET, BODY, now=NOW)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(
            SECRET, b'{"name": "Evil"}', headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER],
            300, now=NOW,
        )


def test_wrong_secret_is_rejected():
    headers = sign_headers("other-secret", BODY, now=NOW)
    with pytest.raises(SignatureError):
        verify_signature(
            SECRET, BODY, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], 300, now=NOW
        )


def test_replayed_old_request_is_rejected():
    headers = sign_headers(SECRET, BODY, now=NOW)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_signature(
            SECRET, BODY, headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER], 300,
            now=NOW + 301,
        )


def test_timestamp_cannot_be_swapped_without_resigning():
    # Attacker captures an old request and only updates the timestamp header.
    old_signature = compute_signature(SECRET, str(NOW - 3600), BODY)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(SECRET, BODY, str(NOW), old_signature, 300, now=NOW)


@pytest.mark.parametrize(
    ("timestamp", "signature", "reason"),
    [(None, "sha256=abc", "missing"), ("123", None, "missing"), ("soon", "sha256=x", "integer")],
)
def test_malformed_headers_are_rejected(timestamp, signature, reason):
    with pytest.raises(SignatureError, match=reason):
        verify_signature(SECRET, BODY, timestamp, signature, 300, now=NOW)
