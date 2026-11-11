"""Background worker that drains the outbox and POSTs leads to destination webhooks.

Retry policy:
- 2xx                       -> delivered
- 408, 429, 5xx, network    -> retry later with exponential backoff + jitter
- any other 4xx             -> dead immediately (the request itself is wrong; retrying won't help)
- attempts >= max_attempts  -> dead (the "dead-letter" state; fix and use /redeliver)
"""

import asyncio
import contextlib
import logging
import random
from datetime import datetime, timedelta

import httpx

from lead_router.config import Settings
from lead_router.db import Database
from lead_router.models import OutboxItem, OutboxStatus, utcnow
from lead_router.security import sign_headers

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429}


def compute_backoff(
    attempt: int, base: float, cap: float, rng: random.Random | None = None
) -> float:
    """Seconds to wait before retry number `attempt` (1-based).

    Exponential growth (base, 2*base, 4*base, ...) capped at `cap`, with "equal jitter":
    half the delay is fixed and half is random. Jitter stops many failed deliveries from
    retrying in lock-step and hammering a destination that is just recovering.
    """
    rng = rng or random.Random()
    delay = min(cap, base * 2 ** (attempt - 1))
    return delay / 2 + rng.uniform(0, delay / 2)


def is_retryable(status_code: int) -> bool:
    """Whether an HTTP error status is worth retrying."""
    return status_code in RETRYABLE_STATUS or status_code >= 500


class DeliveryWorker:
    """Polls the outbox for due rows and delivers them one by one."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.delivery_timeout_seconds)
        self.rng = rng or random.Random()

    def _headers(self, item: OutboxItem) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Same key on every retry of this lead->destination pair, so the receiver can
            # safely ignore a repeat if our earlier attempt actually succeeded.
            "Idempotency-Key": item.idempotency_key,
            "X-Lead-Id": item.lead_id,
        }
        secret = self.settings.outbound_signing_secret
        if secret is not None:
            headers.update(sign_headers(secret.get_secret_value(), item.payload.encode()))
        return headers

    async def deliver(self, item: OutboxItem, now: datetime | None = None) -> OutboxStatus:
        """Attempt one delivery and persist the outcome. Returns the new outbox status."""
        now = now or utcnow()
        attempt = item.attempts + 1
        log_ctx = {"lead_id": item.lead_id, "destination": item.destination, "attempt": attempt}

        error: str | None = None
        retryable = True
        try:
            response = await self.client.post(
                item.url, content=item.payload.encode(), headers=self._headers(item)
            )
            if response.is_success:
                self.db.update_outbox(item.id, OutboxStatus.DELIVERED, attempt, now, None, now)
                logger.info("lead delivered", extra={**log_ctx, "status": response.status_code})
                return OutboxStatus.DELIVERED
            error = f"HTTP {response.status_code}"
            retryable = is_retryable(response.status_code)
        except httpx.HTTPError as exc:  # timeouts, DNS failures, refused connections...
            error = f"{type(exc).__name__}: {exc}"

        if not retryable or attempt >= self.settings.delivery_max_attempts:
            self.db.update_outbox(item.id, OutboxStatus.DEAD, attempt, now, error, now)
            logger.error("delivery dead-lettered", extra={**log_ctx, "error": error})
            return OutboxStatus.DEAD

        delay = compute_backoff(
            attempt,
            self.settings.delivery_base_delay_seconds,
            self.settings.delivery_max_delay_seconds,
            self.rng,
        )
        next_at = now + timedelta(seconds=delay)
        self.db.update_outbox(item.id, OutboxStatus.PENDING, attempt, next_at, error, now)
        logger.warning(
            f"delivery failed, retrying in {delay:.1f}s", extra={**log_ctx, "error": error}
        )
        return OutboxStatus.PENDING

    async def run_once(self, now: datetime | None = None) -> int:
        """Deliver every outbox row that is due. Returns how many rows were attempted."""
        now = now or utcnow()
        items = self.db.due_outbox(now, self.settings.worker_batch_size)
        for item in items:
            await self.deliver(item, now)
        for lead_id in {item.lead_id for item in items}:
            self.db.refresh_lead_status(lead_id)
        return len(items)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. Errors are logged so one bad cycle can't kill the worker."""
        logger.info("delivery worker started")
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("delivery cycle crashed")
            # Sleep for the poll interval, but wake immediately if asked to stop.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.settings.worker_poll_interval_seconds)
        await self.client.aclose()
        logger.info("delivery worker stopped")
