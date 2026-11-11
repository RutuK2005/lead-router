import json
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def load_sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def meta_payload() -> dict:
    return load_sample("meta_lead")


@pytest.fixture
def website_payload() -> dict:
    return load_sample("website_lead")


@pytest.fixture
def generic_payload() -> dict:
    return load_sample("generic_lead")


@pytest.fixture
def spam_payload() -> dict:
    return load_sample("spam_lead")
