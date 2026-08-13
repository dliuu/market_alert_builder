"""Every stored brief body must validate against the *canonical* JSON Schema —
not merely against the Pydantic models generated from it.

The two are not the same check, and the gap is exactly the drift docs/02 warns
about. `datamodel-codegen` types a non-required property as `T | None = None`,
so Pydantic happily accepts (and `model_dump` happily emits) an explicit `null`
where the schema says `"type": "object"` or an enum. The web renderer reads the
body through `json-schema-to-typescript`, which follows the *schema*. So a body
can round-trip through the worker forever and still be a shape the contract does
not describe.

This test is the only place both halves of the contract are checked against the
same bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

_SCHEMA = Path(__file__).parents[3] / "packages" / "contracts" / "brief-object.schema.json"
_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize(
    "fixture_name",
    ["close_brief.json", "close_brief_v2.json", "open_brief.json"],
)
def test_fixture_validates_against_the_canonical_schema(
    validator: Draft202012Validator, fixture_name: str
) -> None:
    body = json.loads((_FIXTURES / fixture_name).read_text())

    errors = [
        f"{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in validator.iter_errors(body)
    ]
    assert not errors, "\n".join(errors)


def test_an_open_body_carries_no_pnl(validator: Draft202012Validator) -> None:
    """The point of the v3 bump, asserted on the bytes that actually get stored
    rather than on the in-memory object."""
    body = json.loads((_FIXTURES / "open_brief.json").read_text())
    assert body.get("book") is None
    assert body["kind"] == "open"
