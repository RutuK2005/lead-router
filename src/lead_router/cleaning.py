"""Field normalisation: whitespace, name casing, E.164 phones and validated emails.

Invalid values are dropped (set to `None`) and recorded in `lead.issues`, rather than
rejecting the whole lead. A lead with a bad email but a good phone is still worth a call.
"""

import re

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from lead_router.models import Lead

_WHITESPACE = re.compile(r"\s+")
TEXT_FIELDS = ("name", "city", "interest", "message")


def clean_text(value: str | None) -> str | None:
    """Collapse runs of whitespace and strip; empty strings become `None`."""
    if value is None:
        return None
    cleaned = _WHITESPACE.sub(" ", value).strip()
    return cleaned or None


def normalise_name(value: str | None) -> str | None:
    """Title-case a person's name: `"  riya  SHARMA-patil "` -> `"Riya Sharma-Patil"`."""
    text = clean_text(value)
    if text is None:
        return None
    words = []
    for word in text.split(" "):
        words.append("-".join(part.capitalize() for part in word.split("-")))
    return " ".join(words)


def normalise_phone(value: str | None, default_region: str = "IN") -> str | None:
    """Return the phone in E.164 format (e.g. `+919812345678`), or `None` if invalid.

    Numbers without a country code are interpreted in `default_region`.
    """
    text = clean_text(value)
    if text is None:
        return None
    try:
        number = phonenumbers.parse(text, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def normalise_email(value: str | None) -> str | None:
    """Return a normalised email address, or `None` if it isn't syntactically valid.

    Deliverability (DNS) checks are disabled so this stays fast and works offline.
    """
    text = clean_text(value)
    if text is None:
        return None
    try:
        result = validate_email(text, check_deliverability=False)
    except EmailNotValidError:
        return None
    return result.normalized.lower()


def clean_lead(lead: Lead, default_region: str = "IN") -> Lead:
    """Return a copy of `lead` with all fields normalised and problems listed in `issues`."""
    updates: dict[str, object] = {field: clean_text(getattr(lead, field)) for field in TEXT_FIELDS}
    updates["name"] = normalise_name(lead.name)
    issues = list(lead.issues)

    updates["phone"] = normalise_phone(lead.phone, default_region)
    if lead.phone and updates["phone"] is None:
        issues.append(f"invalid phone: {lead.phone!r}")

    updates["email"] = normalise_email(lead.email)
    if lead.email and updates["email"] is None:
        issues.append(f"invalid email: {lead.email!r}")

    if not updates["phone"] and not updates["email"]:
        issues.append("no valid contact method")

    updates["issues"] = issues
    return lead.model_copy(update=updates)
