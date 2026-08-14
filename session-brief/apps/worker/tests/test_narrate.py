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
    build_open_prompt,
    build_prompt,
    narrate_and_apply,
    narrate_open_and_apply,
    parse_narration,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "close_brief.json"


def _obj() -> BriefObject:
    return BriefObject.model_validate(json.loads(_FIXTURE.read_text()))


def _why(obj: BriefObject, symbol: str) -> str | None:
    section = next(s for s in obj.sections if s.id.value == "attribution")
    row = next(r for r in section.rows if r.symbol == symbol)
    return row.why


def _obj_with_attribution(rows: list[dict[str, object]]) -> BriefObject:
    """Build a BriefObject whose attribution rows carry the given resid_z
    (and optionally theme_bps/resid_bps), for prompt-salience tests."""
    payload = json.loads(_FIXTURE.read_text())
    base_row = payload["sections"][0]["rows"][0]
    payload["sections"][0]["rows"] = [
        {
            **base_row,
            "symbol": row["symbol"],
            "resid_z": row.get("resid_z"),
            "theme_bps": row.get("theme_bps"),
            "resid_bps": row.get("resid_bps"),
        }
        for row in rows
    ]
    return BriefObject.model_validate(payload)


# --- build_prompt ---------------------------------------------------------


def test_prompt_lists_the_attribution_symbols_and_forbids_numbers() -> None:
    prompt = build_prompt(_obj())
    assert "'A'" in prompt and "'B'" in prompt  # both attribution rows named
    assert "prose only" in prompt.lower()


def test_build_prompt_names_residual_lead() -> None:
    obj = _obj_with_attribution(
        [
            {"symbol": "AAA", "resid_z": 0.4},
            {"symbol": "BBB", "resid_z": 3.5},
            {"symbol": "CCC", "resid_z": -1.1},
        ]
    )
    prompt = build_prompt(obj)
    assert "BBB" in prompt
    # The lead clause itself must single out the top-|resid_z| name.
    assert "It must be about BBB, today's single largest idiosyncratic mover." in prompt


def test_build_prompt_lead_clause_picks_max_resid_z_not_symbol_order() -> None:
    # AAA is first in row order and alphabetically first; MMM is alphabetically
    # between; ZZZ has the largest |resid_z| but is last in both row order and
    # alphabetical order. Only a true max-|resid_z| selection lands on ZZZ —
    # "first row" or "first alphabetically" regressions would land on AAA.
    obj = _obj_with_attribution(
        [
            {"symbol": "AAA", "resid_z": 0.4},
            {"symbol": "MMM", "resid_z": 1.0},
            {"symbol": "ZZZ", "resid_z": 3.5},
        ]
    )
    prompt = build_prompt(obj)
    assert "It must be about ZZZ, today's single largest idiosyncratic mover." in prompt
    assert "about AAA" not in prompt
    assert "about MMM" not in prompt


def test_build_prompt_omits_lead_clause_when_no_row_has_resid_z() -> None:
    obj = _obj_with_attribution(
        [{"symbol": "AAA", "resid_z": None}, {"symbol": "BBB", "resid_z": None}]
    )
    prompt = build_prompt(obj)  # must not raise
    assert "It must be about" not in prompt


def test_build_prompt_still_lists_all_symbols_for_why() -> None:
    obj = _obj_with_attribution(
        [{"symbol": "AAA", "resid_z": 1.0}, {"symbol": "BBB", "resid_z": 2.0}]
    )
    prompt = build_prompt(obj)
    assert "AAA" in prompt and "BBB" in prompt


def test_parse_still_drops_digits() -> None:  # guard unchanged
    obj = _obj_with_attribution([{"symbol": "AAA", "resid_z": 2.0}])
    parsed = parse_narration('{"one_thing":"Up 5% today","why":{}}', obj)
    assert parsed.one_thing is None


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


# --- The open brief's forward-looking prompt (M14) -------------------------


def _open_obj() -> BriefObject:
    """A minimal open brief: no book, no attribution section."""
    return BriefObject.model_validate(
        {
            "schema_version": 3,
            "brief_id": "u-2026-08-13-open",
            "user_id": "u",
            "session_date": "2026-08-13",
            "kind": "open",
            "generated_at": "2026-08-13T12:15:00Z",
            "subject": "Open - Thu Aug 13 - the day ahead",
            "one_thing": None,
            "sections": [
                {
                    "id": "calendar",
                    "tier": "full",
                    "note": None,
                    "rows": [
                        {
                            "symbol": "SNDK",
                            "label": "SNDK Q2 earnings",
                            "event_type": "earnings",
                            "occurs_at": "2026-08-13",
                            "tag": "holding",
                        }
                    ],
                }
            ],
            "flags": [],
            "claims": [],
            "resolved_claims": [],
            "suppressed": [],
            "data_quality": {"missing": [], "stale": []},
        }
    )


def test_open_prompt_asks_for_a_forward_looking_read() -> None:
    prompt = build_open_prompt(_open_obj())
    assert "close brief" not in prompt.lower()
    assert "ahead" in prompt.lower()


def test_open_prompt_does_not_ask_for_per_name_why() -> None:
    """docs/05 gives the open brief's sections no `why` line — asking for one
    would invite prose the object has nowhere to put.

    Only the *instruction* is checked: the serialized object appended below it
    legitimately contains a null `why` key on every row.
    """
    instruction = build_open_prompt(_open_obj()).split("for context only")[0]
    assert '"why"' not in instruction


def test_open_narration_fills_one_thing() -> None:
    out = narrate_open_and_apply(
        _open_obj(), lambda _p: '{"one_thing": "Earnings land before the bell."}'
    )
    assert out.one_thing == "Earnings land before the bell."


def test_open_narration_still_drops_digits() -> None:
    """Invariant 2 holds on both briefs."""
    out = narrate_open_and_apply(
        _open_obj(), lambda _p: '{"one_thing": "Semis are up 3 percent."}'
    )
    assert out.one_thing is None


def test_open_narration_is_non_fatal() -> None:
    """DoD 4: a keyless or failing run still yields a valid, sendable brief."""
    obj = _open_obj()
    assert narrate_open_and_apply(obj, None) == obj

    def revoked(_prompt: str) -> str:
        raise RuntimeError("401 Unauthorized")

    assert narrate_open_and_apply(obj, revoked) == obj
