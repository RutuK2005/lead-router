from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from lead_router.config import Settings
from lead_router.llm import LLMScorer, render_lead
from lead_router.models import Classification, Intent, InterestCategory, Lead
from lead_router.scoring import RuleBasedScorer, build_scorer


def make_lead(**overrides) -> Lead:
    fields = {
        "source": "generic",
        "name": "Ananya Rao",
        "phone": "+919876543210",
        "email": "ananya.rao@example.com",
        "city": "Pune",
        "interest": "Clear aligners",
        "message": "Thinking about aligners.",
    }
    fields.update(overrides)
    return Lead(**fields)


# ---- rule-based fallback -------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "intent"),
    [
        ({"message": "What is the price? Can I book this week?"}, Intent.HOT),
        ({"message": "What is the price?", "phone": None}, Intent.WARM),
        ({}, Intent.WARM),
        ({"phone": None, "interest": None, "message": None}, Intent.COLD),
        ({"phone": None, "email": None}, Intent.SPAM),
        ({"message": "We sell SEO backlinks, see https://example.com"}, Intent.SPAM),
    ],
)
def test_rule_based_intent(overrides, intent):
    result = RuleBasedScorer().score(make_lead(**overrides))
    assert result.classification.intent is intent
    assert result.scored_by == "rules"


def test_rule_based_category_and_text():
    result = RuleBasedScorer().score(make_lead(interest="Hair PRP", message=None))
    assert result.classification.interest_category is InterestCategory.HAIR
    assert "Ananya Rao from Pune" in result.classification.summary
    assert result.classification.suggested_follow_up


def test_rule_based_is_deterministic():
    lead = make_lead()
    assert RuleBasedScorer().score(lead) == RuleBasedScorer().score(lead)


def test_build_scorer_without_key_uses_rules():
    settings = Settings(_env_file=None, anthropic_api_key=None)
    assert isinstance(build_scorer(settings), RuleBasedScorer)


def test_build_scorer_with_key_uses_llm():
    settings = Settings(_env_file=None, anthropic_api_key="sk-test-not-real")
    assert isinstance(build_scorer(settings), LLMScorer)


# ---- LLM path (mocked client, no network) ----------------------------------------------


class FakeMessages:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(response=None, error=None) -> SimpleNamespace:
    return SimpleNamespace(messages=FakeMessages(response, error))


LLM_RESULT = Classification(
    intent=Intent.HOT,
    interest_category=InterestCategory.DENTAL,
    summary="Ananya from Pune wants aligners soon.",
    suggested_follow_up="Call today to book a scan.",
)


def test_llm_scorer_returns_parsed_output():
    client = fake_client(SimpleNamespace(stop_reason="end_turn", parsed_output=LLM_RESULT))
    result = LLMScorer(client, model="claude-haiku-5-5").score(make_lead())

    assert result.classification == LLM_RESULT
    assert result.scored_by == "llm:claude-haiku-5-5"
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_format"] is Classification
    assert "<lead>" in call["messages"][0]["content"]


def test_llm_scorer_falls_back_on_api_error():
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://test.invalid"))
    result = LLMScorer(fake_client(error=error), model="m").score(make_lead())
    assert result.scored_by == "rules"


def test_llm_scorer_falls_back_on_refusal():
    client = fake_client(SimpleNamespace(stop_reason="refusal", parsed_output=None))
    assert LLMScorer(client, model="m").score(make_lead()).scored_by == "rules"


def test_llm_scorer_skips_api_for_obvious_spam():
    client = fake_client(SimpleNamespace(stop_reason="end_turn", parsed_output=LLM_RESULT))
    result = LLMScorer(client, model="m").score(make_lead(phone=None, email=None))
    assert result.classification.intent is Intent.SPAM
    assert client.messages.calls == []


def test_render_lead_hides_contact_details():
    text = render_lead(make_lead())
    assert "+919876543210" not in text
    assert "ananya.rao@example.com" not in text
    assert '"has_phone": true' in text
