"""Domain models: the canonical `Lead`, its LLM/rule `Classification`, and API response shapes."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def new_lead_id() -> str:
    """Generate a short, URL-safe, unique lead id such as `ld_3f9a1c2b7e4d`."""
    return f"ld_{uuid.uuid4().hex[:12]}"


class Intent(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    SPAM = "spam"


class InterestCategory(StrEnum):
    DENTAL = "dental"
    DERMATOLOGY = "dermatology"
    HAIR = "hair"
    COSMETIC = "cosmetic"
    WEIGHT_LOSS = "weight_loss"
    FERTILITY = "fertility"
    GENERAL = "general"


class LeadStatus(StrEnum):
    PENDING = "pending"  # waiting in the outbox for delivery
    DELIVERED = "delivered"  # every destination accepted it
    FAILED = "failed"  # at least one destination dead-lettered
    STORED = "stored"  # kept but intentionally not forwarded (spam / no destinations)
    DUPLICATE = "duplicate"  # same phone/email seen inside the dedupe window


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD = "dead"


class Classification(BaseModel):
    """Structured triage result. This exact schema is sent to Claude as the output format."""

    intent: Intent = Field(
        description=(
            "hot = ready to book soon, warm = interested, cold = vague or early research, "
            "spam = not a genuine patient enquiry"
        )
    )
    interest_category: InterestCategory = Field(description="Best-matching treatment category")
    summary: str = Field(description="One-sentence summary of who the lead is and what they want")
    suggested_follow_up: str = Field(description="Concrete next action for the clinic's team")


class Lead(BaseModel):
    """Canonical lead. Adapters produce it, cleaning normalises it, scoring classifies it."""

    id: str = Field(default_factory=new_lead_id)
    source: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    interest: str | None = None
    message: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utcnow)

    # Filled in later in the pipeline.
    issues: list[str] = Field(default_factory=list)
    classification: Classification | None = None
    scored_by: str | None = None
    status: LeadStatus = LeadStatus.PENDING
    duplicate_of: str | None = None

    def outbound_payload(self) -> dict[str, Any]:
        """The JSON body sent to destinations. Excludes the raw inbound payload."""
        return {
            "event": "lead.created",
            "lead": self.model_dump(mode="json", exclude={"raw_payload", "status"}),
        }


class OutboxItem(BaseModel):
    """One pending (or finished) delivery of a lead to one destination."""

    id: int
    lead_id: str
    destination: str
    url: str
    payload: str
    idempotency_key: str
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None = None
    updated_at: datetime


class IngestResult(BaseModel):
    lead_id: str
    status: LeadStatus
    intent: Intent | None = None
    duplicate_of: str | None = None


class LeadPage(BaseModel):
    items: list[Lead]
    total: int
    limit: int
    offset: int


class LeadDetail(BaseModel):
    lead: Lead
    deliveries: list[OutboxItem]
