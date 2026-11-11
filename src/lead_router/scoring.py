"""Lead scoring: a deterministic rule-based scorer, and the factory that picks a scorer.

The rule-based scorer is always available. It runs when no Anthropic API key is set, when
the LLM call fails, and for leads that are obviously spam (no point paying for an LLM call).
"""

from dataclasses import dataclass
from typing import Protocol

from lead_router.config import Settings
from lead_router.models import Classification, Intent, InterestCategory, Lead

HOT_PHRASES = (
    "today", "tomorrow", "asap", "urgent", "this week", "book", "appointment",
    "slot", "available", "price", "cost", "how much", "emi", "consultation",
)
SPAM_PHRASES = (
    "seo", "backlink", "crypto", "bitcoin", "loan offer", "casino", "guest post",
    "marketing services", "website development", "http://", "https://", "www.",
)
CATEGORY_KEYWORDS: dict[InterestCategory, tuple[str, ...]] = {
    InterestCategory.DENTAL: (
        "dental", "teeth", "tooth", "braces", "aligner", "root canal", "implant", "smile",
    ),
    InterestCategory.DERMATOLOGY: ("skin", "acne", "pigmentation", "derma", "laser", "scar"),
    InterestCategory.HAIR: ("hair", "prp", "transplant", "dandruff", "bald"),
    InterestCategory.COSMETIC: ("botox", "filler", "rhinoplasty", "cosmetic", "liposuction"),
    InterestCategory.WEIGHT_LOSS: ("weight", "obesity", "bariatric", "diet"),
    InterestCategory.FERTILITY: ("ivf", "fertility", "iui", "pregnan"),
}
FOLLOW_UPS = {
    Intent.HOT: "Call within 15 minutes and offer the next available consultation slot.",
    Intent.WARM: "Send treatment details and pricing on WhatsApp, then call within 24 hours.",
    Intent.COLD: "Add to the nurture sequence with educational content; no call needed yet.",
    Intent.SPAM: "No action. Keep for review only.",
}


@dataclass(frozen=True)
class ScoreResult:
    classification: Classification
    scored_by: str


class Scorer(Protocol):
    """Anything that can classify a cleaned lead."""

    def score(self, lead: Lead) -> ScoreResult: ...


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def detect_category(text: str) -> InterestCategory:
    """Pick the first treatment category whose keywords appear in `text`."""
    lowered = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if _contains_any(lowered, keywords):
            return category
    return InterestCategory.GENERAL


def is_obvious_spam(lead: Lead) -> bool:
    """True when a lead has no way to contact them, or contains typical spam phrases."""
    if not lead.phone and not lead.email:
        return True
    text = " ".join(filter(None, [lead.name, lead.interest, lead.message])).lower()
    return _contains_any(text, SPAM_PHRASES)


class RuleBasedScorer:
    """Keyword heuristics. Deterministic, free and fast, but blind to nuance."""

    name = "rules"

    def score(self, lead: Lead) -> ScoreResult:
        """Classify a lead using keyword rules only."""
        message = (lead.message or "").lower()
        category = detect_category(f"{lead.interest or ''} {message}")

        if is_obvious_spam(lead):
            intent = Intent.SPAM
        elif _contains_any(message, HOT_PHRASES):
            # Urgency without a phone number is hard to act on quickly, so cap it at warm.
            intent = Intent.HOT if lead.phone else Intent.WARM
        elif lead.phone and (lead.interest or lead.message):
            intent = Intent.WARM
        else:
            intent = Intent.COLD

        who = lead.name or "Unknown lead"
        where = f" from {lead.city}" if lead.city else ""
        what = lead.interest or category.value.replace("_", " ")
        summary = f"{who}{where} enquiring about {what}."
        if intent is Intent.SPAM:
            summary = f"{who}: looks like spam or has no valid contact details."

        classification = Classification(
            intent=intent,
            interest_category=category,
            summary=summary,
            suggested_follow_up=FOLLOW_UPS[intent],
        )
        return ScoreResult(classification=classification, scored_by=self.name)


def build_scorer(settings: Settings) -> Scorer:
    """Return an LLM scorer (with rule-based fallback) if an API key is set, else rules only."""
    fallback = RuleBasedScorer()
    if settings.anthropic_api_key is None:
        return fallback

    import anthropic  # imported lazily so the rules-only path never needs the SDK configured

    from lead_router.llm import LLMScorer

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
    )
    return LLMScorer(client=client, model=settings.llm_model, fallback=fallback)
