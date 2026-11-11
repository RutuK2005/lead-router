"""Sign a sample payload with HMAC-SHA256 and POST it to a running lead-router.

Examples:
    python scripts/send_sample.py meta
    python scripts/send_sample.py website --url http://localhost:8000
    python scripts/send_sample.py generic --file samples/spam_lead.json

The secret is taken from --secret, else from WEBHOOK_SECRETS in the environment, else from
WEBHOOK_SECRETS in a local .env file.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = {
    "meta": "meta_lead.json",
    "website": "website_lead.json",
    "generic": "generic_lead.json",
}


def load_secret(source: str) -> str | None:
    """Find the signing secret for `source` in the environment or the .env file."""
    raw = os.environ.get("WEBHOOK_SECRETS")
    env_file = ROOT / ".env"
    if raw is None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("WEBHOOK_SECRETS="):
                raw = line.split("=", 1)[1].strip().strip("'")
    if not raw:
        return None
    return json.loads(raw).get(source)


def sign(secret: str, body: bytes) -> dict[str, str]:
    """Same scheme as the server: HMAC-SHA256 over "<timestamp>.<body>"."""
    timestamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return {"X-Timestamp": timestamp, "X-Signature": "sha256=" + digest.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=sorted(DEFAULT_FILES), help="webhook source name")
    parser.add_argument("--file", type=Path, help="payload JSON (defaults to the source sample)")
    parser.add_argument("--url", default="http://localhost:8000", help="lead-router base URL")
    parser.add_argument("--secret", help="signing secret (overrides env/.env)")
    args = parser.parse_args()

    secret = args.secret or load_secret(args.source)
    if not secret:
        print(f"No secret for '{args.source}'. Set WEBHOOK_SECRETS or pass --secret.")
        return 1

    path = args.file or ROOT / "samples" / DEFAULT_FILES[args.source]
    # Re-serialise compactly; the exact bytes we sign are the bytes we send.
    body = json.dumps(json.loads(path.read_text(encoding="utf-8"))).encode()
    headers = {"Content-Type": "application/json", **sign(secret, body)}

    response = requests.post(
        f"{args.url}/webhooks/{args.source}", data=body, headers=headers, timeout=30
    )
    print(response.status_code, response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
