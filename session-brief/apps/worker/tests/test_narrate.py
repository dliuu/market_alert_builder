"""Stage ⑤ narration: pure, no network. Covers the merge, the two parser rules
(no numbers, no stray keys), and — the M8 definition of done — that a failing or
absent narrator still yields a valid, sendable brief."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.brief import BriefObject
from worker.narrate import (
    apply_narration,
    build_prompt,
    narrate_and_apply,
    parse_narration,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "close_brief.json"


def _obj() -> BriefObject:
    return BriefObject.model_validate(json.loads(_FIXTURE.read_text()))


def _why(obj: BriefObject, symbol: str) -> str | None:
    section = next(s for s in obj.sections if s.id.value == "attribution")
    row = next(r for r in section.rows if r.symbol == symbol)
    return row.why


# --- build_prompt ---------------------------------------------------------


def test_prompt_lists_the_attribution_symbols_and_forbids_numbers() -> None:
    prompt = build_prompt(_obj())
    assert "'A'" in prompt and "'B'" in prompt  # both attribution rows named
    assert "prose only" in prompt.lower()


# --- parse_narration ------------------------------------------------------


def test_parse_keeps_clean_prose_for_known_symbols() -> None:
    raw = json.dumps(
        {"one_thing": "A carried a red tape.", "why": {"A": "It gapped up on the print."}}
    )
    narration = parse_narration(raw, _obj())
    assert narration.one_thing == "A carried a red tape."
    assert narration.why == {"A": "It gapped up on the print."}


def test_parse_drops_values_with_digits() -> None:
    # A digit anywhere is a hallucinated figure (invariant 2) — drop it.
    raw = json.dumps(
        {
            "one_thing": "The book rose 3 percent.",
            "why": {"A": "Up on strength.", "B": "Down 5 bps."},
        }
    )
    narration = parse_narration(raw, _obj())
    assert narration.one_thing is None  # had a digit
    assert narration.why == {"A": "Up on strength."}  # B's digit-laden line dropped


def test_parse_drops_unknown_symbols() -> None:
    raw = json.dumps({"why": {"A": "Real name.", "ZZZ": "Not in the book."}})
    narration = parse_narration(raw, _obj())
    assert narration.why == {"A": "Real name."}


def test_parse_raises_on_malformed_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_narration("not json {", _obj())


# --- apply_narration ------------------------------------------------------


def test_apply_merges_prose_and_leaves_a_valid_object() -> None:
    narration = parse_narration(
        json.dumps({"one_thing": "One name did the work.", "why": {"A": "Led the tape."}}), _obj()
    )
    out = apply_narration(_obj(), narration)
    assert out.one_thing == "One name did the work."
    assert _why(out, "A") == "Led the tape."
    assert _why(out, "B") is None  # rows without a why stay null — still valid


# --- narrate_and_apply (the seam) -----------------------------------------


def test_no_narrator_returns_the_object_unchanged() -> None:
    out = narrate_and_apply(_obj(), None)
    assert out.one_thing is None


def test_narrator_prose_appears() -> None:
    def fake(_prompt: str) -> str:
        return json.dumps({"one_thing": "SNDK carried it.", "why": {"A": "It printed a beat."}})

    out = narrate_and_apply(_obj(), fake)
    assert out.one_thing == "SNDK carried it."
    assert _why(out, "A") == "It printed a beat."


def test_a_raising_narrator_is_non_fatal() -> None:
    # This is the "revoke the API key" path: an auth/network failure surfaces as
    # an exception from the narrator, and the brief must survive it unchanged.
    def revoked(_prompt: str) -> str:
        raise RuntimeError("401 Unauthorized")

    out = narrate_and_apply(_obj(), revoked)
    assert out.one_thing is None
    assert _why(out, "A") is None


def test_malformed_narrator_output_is_non_fatal() -> None:
    out = narrate_and_apply(_obj(), lambda _p: "garbage, not json")
    assert out.one_thing is None
