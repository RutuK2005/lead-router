"""HMAC-SHA256 webhook signatures with a timestamp to block replays.

Scheme (used for both inbound verification and outbound signing):

    X-Timestamp: <unix seconds>
    X-Signature: sha256=<hex(HMAC_SHA256(secret, f"{timestamp}.{raw_body}"))>

Signing the timestamp together with the body means an attacker cannot re-use an old
signature with a fresh timestamp, and the tolerance window rejects old captured requests.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
_PREFIX = "sha256="


class SignatureError(Exception):
    """Raised when a request's signature is missing, stale or does not match."""


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Return the `sha256=<hex>` signature for a timestamp and raw request body."""
    message = timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def sign_headers(secret: str, body: bytes, now: float | None = None) -> dict[str, str]:
    """Build the timestamp + signature headers for an outgoing request."""
    timestamp = str(int(now if now is not None else time.time()))
    return {
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: compute_signature(secret, timestamp, body),
    }


def verify_signature(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> None:
    """Validate a request signature, raising `SignatureError` with a reason on failure.

    Checks, in order: headers present, timestamp is an integer, timestamp is within
    `tolerance_seconds` of now (replay protection), and the HMAC matches.
    """
    if not timestamp or not signature:
        raise SignatureError("missing signature or timestamp header")
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("timestamp is not an integer") from exc

    current = now if now is not None else time.time()
    if abs(current - sent_at) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance window")

    expected = compute_signature(secret, timestamp, body)
    # compare_digest takes the same time whether the first or last byte differs,
    # so response timing can't be used to guess the signature byte by byte.
    if not hmac.compare_digest(expected, signature.strip()):
        raise SignatureError("signature mismatch")
