"""Source adapters: map each source's payload shape onto the canonical `Lead` model.

Adapters only *map* fields. They don't validate phones or emails; that is `cleaning.py`'s job,
so every source gets exactly the same normalisation rules.
"""

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from lead_router.models import Lead

Payload = dict[str, Any]
Adapter = Callable[[Payload], Lead]


class AdapterError(ValueError):
    """Raised when a payload doesn't have the shape its source adapter expects."""


def _first(data: Payload, *keys: str) -> str | None:
    """Return the first non-empty value among `keys`, converted to a string."""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def meta_adapter(payload: Payload) -> Lead:
    """Meta (Facebook/Instagram) lead ad, in the shape returned by the Graph API lead lookup.

    Form answers arrive as a list: `[{"name": "full_name", "values": ["..."]}, ...]`.
    """
    field_data = payload.get("field_data")
    if not isinstance(field_data, list):
        raise AdapterError("meta payload must contain a 'field_data' list")

    fields: Payload = {}
    for item in field_data:
        if isinstance(item, dict) and item.get("values"):
            fields[str(item.get("name", "")).lower()] = item["values"][0]

    return Lead(
        source="meta",
        name=_first(fields, "full_name", "name", "first_name"),
        phone=_first(fields, "phone_number", "phone"),
        email=_first(fields, "email"),
        city=_first(fields, "city"),
        interest=_first(fields, "treatment", "which_treatment_are_you_interested_in", "service"),
        message=_first(fields, "message", "comments"),
        utm_source="facebook",
        utm_medium="paid_social",
        utm_campaign=_first(payload, "campaign_name"),
        utm_content=_first(payload, "ad_name"),
        raw_payload=payload,
    )


def website_adapter(payload: Payload) -> Lead:
    """Website contact form: `{"page_url": "...?utm_source=...", "fields": {...}}`.

    UTM parameters are read from the landing page URL's query string.
    """
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        raise AdapterError("website payload must contain a 'fields' object")

    query = parse_qs(urlparse(str(payload.get("page_url", ""))).query)
    utm = {key: values[0] for key, values in query.items() if values}

    return Lead(
        source="website",
        name=_first(fields, "name", "full_name"),
        phone=_first(fields, "phone", "mobile"),
        email=_first(fields, "email"),
        city=_first(fields, "city", "location"),
        interest=_first(fields, "treatment", "service"),
        message=_first(fields, "message", "query"),
        utm_source=utm.get("utm_source"),
        utm_medium=utm.get("utm_medium"),
        utm_campaign=utm.get("utm_campaign"),
        utm_content=utm.get("utm_content"),
        raw_payload=payload,
    )


def generic_adapter(payload: Payload) -> Lead:
    """Flat JSON from any tool (Typeform, n8n, Zapier...). Accepts common key aliases."""
    if not isinstance(payload, dict) or not payload:
        raise AdapterError("generic payload must be a non-empty JSON object")

    return Lead(
        source="generic",
        name=_first(payload, "name", "full_name", "fullName"),
        phone=_first(payload, "phone", "phone_number", "mobile", "whatsapp"),
        email=_first(payload, "email", "email_address"),
        city=_first(payload, "city", "location"),
        interest=_first(payload, "interest", "treatment", "service"),
        message=_first(payload, "message", "notes", "comments"),
        utm_source=_first(payload, "utm_source"),
        utm_medium=_first(payload, "utm_medium"),
        utm_campaign=_first(payload, "utm_campaign"),
        utm_content=_first(payload, "utm_content"),
        raw_payload=payload,
    )


ADAPTERS: dict[str, Adapter] = {
    "meta": meta_adapter,
    "website": website_adapter,
    "generic": generic_adapter,
}


def get_adapter(source: str) -> Adapter | None:
    """Look up the adapter for a source name, or `None` if the source is unknown."""
    return ADAPTERS.get(source)
