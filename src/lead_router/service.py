"""The ingest pipeline: adapt -> clean -> dedupe -> score -> store (with outbox rows)."""

import logging
from datetime import timedelta
from typing import Any

from lead_router.adapters import AdapterError, get_adapter
from lead_router.cleaning import clean_lead
from lead_router.config import Settings
from lead_router.db import Database
from lead_router.models import IngestResult, Intent, LeadStatus, utcnow
from lead_router.scoring import Scorer

logger = logging.getLogger(__name__)


class UnknownSourceError(LookupError):
    """Raised when no adapter is registered for a webhook source."""


class LeadService:
    """Turns a raw webhook payload into a stored, scored lead queued for delivery."""

    def __init__(self, db: Database, scorer: Scorer, settings: Settings):
        self.db = db
        self.scorer = scorer
        self.settings = settings

    def ingest(self, source: str, payload: dict[str, Any]) -> IngestResult:
        """Process one inbound lead. Raises `UnknownSourceError` or `AdapterError`."""
        adapter = get_adapter(source)
        if adapter is None:
            raise UnknownSourceError(source)
        if not isinstance(payload, dict):
            raise AdapterError("payload must be a JSON object")

        lead = clean_lead(adapter(payload), self.settings.default_region)
        now = utcnow()
        log_ctx = {"lead_id": lead.id, "source": source}

        since = now - timedelta(hours=self.settings.dedupe_window_hours)
        original = self.db.find_duplicate(lead.phone, lead.email, since)
        if original:
            # Store the duplicate for auditing, but don't score it or deliver it again.
            lead = lead.model_copy(
                update={"status": LeadStatus.DUPLICATE, "duplicate_of": original}
            )
            self.db.insert_lead(lead, destinations={}, now=now)
            logger.info("duplicate lead stored", extra={**log_ctx, "status": "duplicate"})
            return IngestResult(lead_id=lead.id, status=lead.status, duplicate_of=original)

        result = self.scorer.score(lead)
        intent = result.classification.intent
        forward = intent is not Intent.SPAM or self.settings.forward_spam
        destinations = self.settings.destinations if forward else {}
        status = LeadStatus.PENDING if destinations else LeadStatus.STORED

        lead = lead.model_copy(
            update={
                "classification": result.classification,
                "scored_by": result.scored_by,
                "status": status,
            }
        )
        self.db.insert_lead(lead, destinations, now)
        logger.info(
            f"lead stored, scored by {result.scored_by}, {len(destinations)} deliveries queued",
            extra={**log_ctx, "intent": intent.value, "status": status.value},
        )
        return IngestResult(lead_id=lead.id, status=status, intent=intent)
