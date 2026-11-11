import pytest

from lead_router.adapters import AdapterError, get_adapter
from lead_router.cleaning import clean_lead, normalise_email, normalise_name, normalise_phone
from lead_router.models import Lead


def test_meta_adapter_maps_field_data(meta_payload):
    lead = get_adapter("meta")(meta_payload)
    assert lead.source == "meta"
    assert lead.name == "  ananya   RAO "  # adapters don't clean
    assert lead.phone == "098765 43210"
    assert lead.interest == "Clear aligners"
    assert lead.utm_source == "facebook"
    assert lead.utm_campaign == "Aligners - Pune - Nov"
    assert lead.raw_payload == meta_payload


def test_website_adapter_reads_utm_from_page_url(website_payload):
    lead = get_adapter("website")(website_payload)
    assert lead.utm_source == "google"
    assert lead.utm_medium == "cpc"
    assert lead.utm_campaign == "prp-nov"
    assert lead.utm_content == "ad-variant-b"
    assert lead.interest == "Hair PRP"


def test_generic_adapter_accepts_aliases(generic_payload):
    lead = get_adapter("generic")(generic_payload)
    assert lead.name == "Meera Iyer"
    assert lead.phone == "9876543212"
    assert lead.email == "meera.iyer@example.net"
    assert lead.city == "Bengaluru"
    assert lead.message.startswith("Interested in laser")


@pytest.mark.parametrize(
    ("source", "payload"),
    [("meta", {"id": "1"}), ("website", {"fields": "nope"}), ("generic", {})],
)
def test_adapters_reject_wrong_shapes(source, payload):
    with pytest.raises(AdapterError):
        get_adapter(source)(payload)


def test_unknown_source_has_no_adapter():
    assert get_adapter("tiktok") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("98765 43210", "+919876543210"),
        ("098765-43210", "+919876543210"),
        ("+91 (98765) 43210", "+919876543210"),
        ("+1 415 555 2671", "+14155552671"),
        ("12345", None),
        ("not a phone", None),
        ("", None),
        (None, None),
    ],
)
def test_normalise_phone(raw, expected):
    assert normalise_phone(raw, "IN") == expected


def test_normalise_email_and_name():
    assert normalise_email("  Ananya.Rao@Example.COM ") == "ananya.rao@example.com"
    assert normalise_email("not-an-email") is None
    assert normalise_name("  riya  SHARMA-patil ") == "Riya Sharma-Patil"
    assert normalise_name("   ") is None


def test_clean_lead_normalises_and_records_issues():
    lead = Lead(source="generic", name=" john  DOE ", phone="123", email="bad@", message="  hi  ")
    cleaned = clean_lead(lead)
    assert cleaned.name == "John Doe"
    assert cleaned.phone is None
    assert cleaned.email is None
    assert cleaned.message == "hi"
    assert "no valid contact method" in cleaned.issues
    assert any("invalid phone" in issue for issue in cleaned.issues)
    assert lead.name == " john  DOE "  # original is not mutated
