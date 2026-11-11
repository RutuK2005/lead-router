import json
import time

import pytest
from fastapi.testclient import TestClient

from lead_router.config import Settings
from lead_router.main import create_app
from lead_router.models import Intent
from lead_router.scoring import RuleBasedScorer
from lead_router.security import sign_headers

SECRETS = {"meta": "meta-secret", "website": "web-secret", "generic": "generic-secret"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "api.db"),
        webhook_secrets=SECRETS,
        meta_verify_token="verify-me",
        destinations={"crm": "https://crm.test/hook"},
        worker_enabled=False,  # tests drive delivery explicitly; no background task
    )


@pytest.fixture
def client(settings):
    app = create_app(settings, scorer=RuleBasedScorer())
    with TestClient(app) as test_client:
        yield test_client


def post_lead(client, source: str, payload: dict, secret: str | None = None, **sign_kwargs):
    body = json.dumps(payload).encode()
    headers = sign_headers(secret or SECRETS[source], body, **sign_kwargs)
    headers["Content-Type"] = "application/json"
    return client.post(f"/webhooks/{source}", content=body, headers=headers)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["scorer"] == "RuleBasedScorer"


def test_meta_verification_handshake(client):
    params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"}
    response = client.get("/webhooks/meta", params=params)
    assert response.status_code == 200
    assert response.text == "1234"

    params["hub.verify_token"] = "wrong"
    assert client.get("/webhooks/meta", params=params).status_code == 403


def test_signed_meta_lead_is_cleaned_scored_and_queued(client, meta_payload):
    response = post_lead(client, "meta", meta_payload)
    assert response.status_code == 202
    result = response.json()
    assert result["status"] == "pending"
    assert result["intent"] == Intent.HOT

    detail = client.get(f"/leads/{result['lead_id']}").json()
    lead = detail["lead"]
    assert lead["name"] == "Ananya Rao"
    assert lead["phone"] == "+919876543210"
    assert lead["email"] == "ananya.rao@example.com"
    assert lead["classification"]["interest_category"] == "dental"
    assert lead["scored_by"] == "rules"
    assert detail["deliveries"][0]["destination"] == "crm"
    assert detail["deliveries"][0]["status"] == "pending"


def test_bad_signature_is_rejected(client, website_payload):
    response = post_lead(client, "website", website_payload, secret="wrong-secret")
    assert response.status_code == 401
    assert client.get("/leads").json()["total"] == 0


def test_replayed_request_is_rejected(client, website_payload):
    response = post_lead(client, "website", website_payload, now=time.time() - 3600)
    assert response.status_code == 401


def test_unknown_source_is_404(client):
    assert post_lead(client, "generic", {"name": "x"}).status_code == 202
    response = client.post("/webhooks/tiktok", content=b"{}")
    assert response.status_code == 404


def test_malformed_payload_is_422(client):
    response = post_lead(client, "meta", {"id": "no field_data"})
    assert response.status_code == 422


def test_duplicate_within_window_is_not_requeued(client, generic_payload):
    first = post_lead(client, "generic", generic_payload).json()
    second = post_lead(client, "generic", generic_payload).json()

    assert second["status"] == "duplicate"
    assert second["duplicate_of"] == first["lead_id"]
    assert client.get(f"/leads/{second['lead_id']}").json()["deliveries"] == []
    # A duplicate has nothing to redeliver.
    assert client.post(f"/leads/{second['lead_id']}/redeliver").status_code == 409


def test_spam_is_stored_but_not_forwarded(client, spam_payload):
    result = post_lead(client, "generic", spam_payload).json()
    assert result["intent"] == "spam"
    assert result["status"] == "stored"


def test_list_filters_pagination_and_metrics(client, meta_payload, website_payload, spam_payload):
    post_lead(client, "meta", meta_payload)
    post_lead(client, "website", website_payload)
    post_lead(client, "generic", spam_payload)

    page = client.get("/leads", params={"limit": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 2

    assert client.get("/leads", params={"source": "website"}).json()["total"] == 1
    assert client.get("/leads", params={"intent": "spam"}).json()["total"] == 1
    assert client.get("/leads", params={"status": "pending"}).json()["total"] == 2
    assert client.get("/leads", params={"intent": "lukewarm"}).status_code == 422

    metrics = client.get("/metrics").json()
    assert metrics["leads_total"] == 3
    assert metrics["leads_by_source"] == {"meta": 1, "website": 1, "generic": 1}
    assert metrics["outbox_by_status"] == {"pending": 2}


def test_redeliver_and_missing_lead(client, meta_payload):
    lead_id = post_lead(client, "meta", meta_payload).json()["lead_id"]
    response = client.post(f"/leads/{lead_id}/redeliver")
    assert response.status_code == 202
    assert response.json()["requeued"] == 1
    assert client.get("/leads/ld_missing").status_code == 404
    assert client.post("/leads/ld_missing/redeliver").status_code == 404


def test_source_without_secret_fails_closed(tmp_path, generic_payload):
    settings = Settings(
        _env_file=None, database_path=str(tmp_path / "x.db"), worker_enabled=False
    )
    with TestClient(create_app(settings, scorer=RuleBasedScorer())) as client:
        response = post_lead(client, "generic", generic_payload)
    assert response.status_code == 401
