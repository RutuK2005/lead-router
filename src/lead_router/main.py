"""FastAPI application: webhook endpoints, lead queries and the delivery worker lifecycle.

Run with:  uvicorn lead_router.main:create_app --factory
"""

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from lead_router import __version__
from lead_router.adapters import AdapterError, get_adapter
from lead_router.config import Settings, get_settings
from lead_router.db import Database
from lead_router.delivery import DeliveryWorker
from lead_router.logging_setup import configure_logging
from lead_router.models import IngestResult, Intent, LeadDetail, LeadPage, LeadStatus, utcnow
from lead_router.scoring import Scorer, build_scorer
from lead_router.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    verify_signature,
)
from lead_router.service import LeadService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, scorer: Scorer | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings/scorer; production reads the environment."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    db = Database(settings.database_path)
    db.init_schema()
    scorer = scorer or build_scorer(settings)
    service = LeadService(db, scorer, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = None
        if settings.worker_enabled:
            task = asyncio.create_task(DeliveryWorker(db, settings).run_forever(stop))
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="lead-router", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness check plus a summary of the active configuration (no secrets)."""
        with db.connect() as conn:
            conn.execute("SELECT 1")
        return {
            "status": "ok",
            "version": __version__,
            "scorer": type(scorer).__name__,
            "destinations": sorted(settings.destinations),
            "worker_enabled": settings.worker_enabled,
        }

    @app.get("/webhooks/meta", response_class=PlainTextResponse)
    def meta_verification(
        mode: str = Query(alias="hub.mode"),
        token: str = Query(alias="hub.verify_token"),
        challenge: str = Query(alias="hub.challenge"),
    ) -> str:
        """Meta's subscription handshake: echo `hub.challenge` if the verify token matches."""
        expected = settings.meta_verify_token
        if (
            mode == "subscribe"
            and expected is not None
            and hmac.compare_digest(token.encode(), expected.get_secret_value().encode())
        ):
            return challenge
        raise HTTPException(status_code=403, detail="verification failed")

    @app.post("/webhooks/{source}", status_code=202, response_model=IngestResult)
    async def receive_webhook(source: str, request: Request) -> IngestResult:
        """Verify the HMAC signature, then clean, score, store and queue the lead."""
        if get_adapter(source) is None:
            raise HTTPException(status_code=404, detail=f"unknown source '{source}'")
        secret = settings.webhook_secrets.get(source)
        if not secret:
            # Fail closed: an unconfigured source must never accept unsigned data.
            raise HTTPException(status_code=401, detail="source has no signing secret configured")

        body = await request.body()  # the raw bytes, exactly as signed by the sender
        try:
            verify_signature(
                secret,
                body,
                request.headers.get(TIMESTAMP_HEADER),
                request.headers.get(SIGNATURE_HEADER),
                settings.signature_tolerance_seconds,
            )
        except SignatureError as exc:
            logger.warning("rejected webhook", extra={"source": source, "error": str(exc)})
            raise HTTPException(status_code=401, detail="invalid signature") from exc

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="body is not valid JSON") from exc

        try:
            # Scoring may call the LLM (blocking I/O), so run it off the event loop.
            return await run_in_threadpool(service.ingest, source, payload)
        except AdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/leads", response_model=LeadPage)
    def list_leads(
        status: LeadStatus | None = None,
        intent: Intent | None = None,
        source: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> LeadPage:
        """List leads, newest first, with optional filters and limit/offset pagination."""
        items, total = db.list_leads(
            status=status.value if status else None,
            intent=intent.value if intent else None,
            source=source,
            limit=limit,
            offset=offset,
        )
        return LeadPage(items=items, total=total, limit=limit, offset=offset)

    @app.get("/leads/{lead_id}", response_model=LeadDetail)
    def get_lead(lead_id: str) -> LeadDetail:
        """One lead with the state of each of its deliveries."""
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="lead not found")
        return LeadDetail(lead=lead, deliveries=db.outbox_for_lead(lead_id))

    @app.post("/leads/{lead_id}/redeliver", status_code=202)
    def redeliver(lead_id: str) -> dict[str, Any]:
        """Re-queue all of a lead's deliveries (e.g. after fixing a dead destination)."""
        if db.get_lead(lead_id) is None:
            raise HTTPException(status_code=404, detail="lead not found")
        requeued = db.reset_outbox(lead_id, utcnow())
        if requeued == 0:
            raise HTTPException(status_code=409, detail="lead has no deliveries to retry")
        logger.info("redelivery requested", extra={"lead_id": lead_id})
        return {"lead_id": lead_id, "requeued": requeued}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        """Counts of leads by status/intent/source and outbox rows by status."""
        return db.metrics()

    return app
