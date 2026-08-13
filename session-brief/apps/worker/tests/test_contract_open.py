"""The v3 contract shape (M14): a `book`-less open brief, the §4/§5 row fields,
and the back-compat guarantee that a stored v2 close body still validates.

These assert the *generated* Pydantic models, so they fail until
`brief-object.schema.json` changes and `pnpm contracts:gen` runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.brief import BriefObject

# A real v2 close-brief body, frozen before the M14 bump and never regenerated.
# Its whole job is to keep proving that a stored old brief still loads (docs/04).
_V2_FIXTURE = Path(__file__).parent / "fixtures" / "close_brief_v2.json"


def _minimal(**overrides: object) -> dict[str, object]:
    """The smallest valid object, with `book` deliberately absent."""
    payload: dict[str, object] = {
        "schema_version": 3,
        "brief_id": "u-2026-08-13-open",
        "user_id": "u",
        "session_date": "2026-08-13",
        "kind": "open",
        "generated_at": "2026-08-13T12:15:00Z",
        "subject": "Open · Thu Aug 13",
        "one_thing": None,
        "sections": [],
        "flags": [],
        "claims": [],
        "resolved_claims": [],
        "suppressed": [],
        "data_quality": {"missing": [], "stale": []},
    }
    payload.update(overrides)
    return payload


# --- `book` becomes optional ----------------------------------------------


def test_open_brief_validates_without_a_book() -> None:
    obj = BriefObject.model_validate(_minimal())
    assert obj.book is None


def test_close_brief_still_carries_its_book() -> None:
    obj = BriefObject.model_validate(
        _minimal(
            kind="close",
            book={
                "value_cents": 1000,
                "day_pnl_cents": 10,
                "day_bps": 100,
                "total_pnl_cents": 50,
            },
        )
    )
    assert obj.book is not None
    assert obj.book.day_bps == 100


# --- Back-compat: a stored v2 body must keep validating -------------------


def test_stored_v2_close_body_still_validates() -> None:
    """docs/04: old briefs re-render under new templates. A v2 body has every
    key v3 has, so widening the contract must not reject it."""
    body = json.loads(_V2_FIXTURE.read_text())
    assert body["schema_version"] == 2

    obj = BriefObject.model_validate(body)

    assert obj.schema_version == 2
    assert obj.book is not None  # a v2 close brief always had one


# --- §4 calendar rows ------------------------------------------------------


def test_calendar_row_carries_its_event_fields() -> None:
    section = {
        "id": "calendar",
        "tier": "full",
        "note": None,
        "rows": [
            {
                "symbol": "SNDK",
                "label": "SanDisk Q3 earnings",
                "event_type": "earnings",
                "occurs_at": "2026-08-13",
                "tag": "holding",
            }
        ],
    }
    obj = BriefObject.model_validate(_minimal(sections=[section]))

    row = obj.sections[0].rows[0]
    assert row.label == "SanDisk Q3 earnings"
    assert row.event_type is not None and row.event_type.value == "earnings"
    assert row.tag is not None and row.tag.value == "holding"


def test_macro_calendar_row_needs_no_symbol() -> None:
    """A CPI print has no ticker — `symbol` must be optional (it was required)."""
    section = {
        "id": "calendar",
        "tier": "full",
        "note": None,
        "rows": [
            {
                "label": "CPI (Jul)",
                "event_type": "macro",
                "occurs_at": "2026-08-13",
                "tag": "macro",
            }
        ],
    }
    obj = BriefObject.model_validate(_minimal(sections=[section]))
    assert obj.sections[0].rows[0].symbol is None


# --- §5 sector-setup rows --------------------------------------------------


def test_sector_setup_row_carries_its_fields() -> None:
    section = {
        "id": "sector_setup",
        "tier": "full",
        "note": None,
        "rows": [
            {
                "sector_id": "s1",
                "name": "Semis",
                "benchmark_symbol": "SMH",
                "ret_5d": 0.031,
                "vs_spy_5d": 0.012,
                "premarket": None,
            }
        ],
    }
    obj = BriefObject.model_validate(_minimal(sections=[section]))

    row = obj.sections[0].rows[0]
    assert row.name == "Semis"
    assert row.benchmark_symbol == "SMH"
    assert row.ret_5d == pytest.approx(0.031)
    assert row.premarket is None  # null until M15


# --- The row def stops silently swallowing typos --------------------------


def test_row_rejects_an_unknown_field() -> None:
    """`row` was the one def without additionalProperties:false, so Pydantic
    *ignored* stray keys instead of rejecting them — a typo shipped silently.

    The payload is otherwise valid (book present, so this can't pass on a
    missing-book error instead), and the assertion names the offending key.
    """
    section = {
        "id": "calendar",
        "tier": "full",
        "note": None,
        "rows": [{"symbol": "SNDK", "occurs_att": "2026-08-13"}],  # typo'd key
    }
    payload = _minimal(
        kind="close",
        book={
            "value_cents": 1000,
            "day_pnl_cents": 10,
            "day_bps": 100,
            "total_pnl_cents": 50,
        },
        sections=[section],
    )

    with pytest.raises(ValidationError) as excinfo:
        BriefObject.model_validate(payload)

    assert "occurs_att" in str(excinfo.value)
