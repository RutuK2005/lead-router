import asyncio
import json
import random
from datetime import timedelta

import httpx
import pytest

from lead_router.config import Settings
from lead_router.db import Database
from lead_router.delivery import DeliveryWorker, compute_backoff, is_retryable
from lead_router.models import Lead, LeadStatus, OutboxStatus, utcnow
from lead_router.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify_signature

DESTINATIONS = {"crm": "https://crm.test/hook"}


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    database.init_schema()
    return database


def make_settings(**overrides) -> Settings:
    values = {
        "delivery_max_attempts": 3,
        "delivery_base_delay_seconds": 2.0,
        "delivery_max_delay_seconds": 60.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_worker(db, handler, **settings) -> tuple[DeliveryWorker, list[httpx.Request]]:
    """Worker whose HTTP client is backed by `httpx.MockTransport` (no network)."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    worker = DeliveryWorker(db, make_settings(**settings), client=client, rng=random.Random(0))
    return worker, seen


def insert_lead(db: Database) -> Lead:
    lead = Lead(source="generic", name="Test Lead", phone="+919876543210")
    db.insert_lead(lead, DESTINATIONS, utcnow())
    return lead


def run(coro):
    return asyncio.run(coro)


def test_backoff_grows_exponentially_and_is_capped():
    rng = random.Random(42)
    delays = [compute_backoff(n, base=2, cap=30, rng=rng) for n in range(1, 7)]
    expected_ceilings = [2, 4, 8, 16, 30, 30]
    for delay, ceiling in zip(delays, expected_ceilings, strict=True):
        assert ceiling / 2 <= delay <= ceiling  # equal jitter: between half and full delay


def test_retryable_status_codes():
    assert is_retryable(500) and is_retryable(503) and is_retryable(429) and is_retryable(408)
    assert not is_retryable(400) and not is_retryable(404) and not is_retryable(422)


def test_successful_delivery_sends_idempotency_key(db):
    lead = insert_lead(db)
    worker, seen = make_worker(db, lambda request: httpx.Response(200))

    assert run(worker.run_once()) == 1

    request = seen[0]
    assert request.headers["Idempotency-Key"] == f"{lead.id}:crm"
    assert request.headers["X-Lead-Id"] == lead.id
    assert json.loads(request.content)["lead"]["id"] == lead.id
    assert db.outbox_for_lead(lead.id)[0].status is OutboxStatus.DELIVERED
    assert db.get_lead(lead.id).status is LeadStatus.DELIVERED


def test_server_error_is_retried_with_backoff_then_succeeds(db):
    lead = insert_lead(db)
    responses = iter([httpx.Response(503), httpx.Response(200)])
    worker, seen = make_worker(db, lambda request: next(responses))
    start = utcnow()

    run(worker.run_once(now=start))
    item = db.outbox_for_lead(lead.id)[0]
    assert item.status is OutboxStatus.PENDING
    assert item.attempts == 1
    assert item.last_error == "HTTP 503"
    assert item.next_attempt_at > start

    # Not due yet: nothing is sent.
    assert run(worker.run_once(now=start)) == 0

    run(worker.run_once(now=start + timedelta(seconds=120)))
    item = db.outbox_for_lead(lead.id)[0]
    assert item.status is OutboxStatus.DELIVERED
    assert item.attempts == 2
    assert len(seen) == 2
    # The idempotency key is stable across retries.
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]


def test_network_errors_dead_letter_after_max_attempts(db):
    lead = insert_lead(db)

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    worker, seen = make_worker(db, refuse, delivery_max_attempts=3)
    now = utcnow()
    for _ in range(5):
        run(worker.run_once(now=now))
        now += timedelta(hours=1)

    item = db.outbox_for_lead(lead.id)[0]
    assert len(seen) == 3
    assert item.status is OutboxStatus.DEAD
    assert "ConnectError" in item.last_error
    assert db.get_lead(lead.id).status is LeadStatus.FAILED


def test_client_error_is_dead_lettered_immediately(db):
    lead = insert_lead(db)
    worker, seen = make_worker(db, lambda request: httpx.Response(400))

    run(worker.run_once())

    assert len(seen) == 1
    assert db.outbox_for_lead(lead.id)[0].status is OutboxStatus.DEAD


def test_reset_outbox_requeues_dead_rows(db):
    lead = insert_lead(db)
    worker, _ = make_worker(db, lambda request: httpx.Response(400))
    run(worker.run_once())

    assert db.reset_outbox(lead.id, utcnow()) == 1
    item = db.outbox_for_lead(lead.id)[0]
    assert item.status is OutboxStatus.PENDING and item.attempts == 0
    assert db.get_lead(lead.id).status is LeadStatus.PENDING


def test_outbound_requests_are_signed_when_secret_set(db):
    insert_lead(db)
    worker, seen = make_worker(
        db, lambda request: httpx.Response(200), outbound_signing_secret="out-secret"
    )
    run(worker.run_once())

    request = seen[0]
    verify_signature(
        "out-secret",
        request.content,
        request.headers[TIMESTAMP_HEADER],
        request.headers[SIGNATURE_HEADER],
        tolerance_seconds=60,
    )
