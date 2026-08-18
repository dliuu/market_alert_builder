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


def _obj_with_breakouts(breakouts: dict[str, str | None]) -> BriefObject:
    """The fixture with §4's `breakout` overridden per symbol."""
    payload = json.loads(_FIXTURE.read_text())
    section = next(s for s in payload["sections"] if s["id"] == "tape_quality")
    for row in section["rows"]:
        row["breakout"] = breakouts.get(row["symbol"])
    return BriefObject.model_validate(payload)


# --- build_prompt ---------------------------------------------------------


def test_prompt_frames_a_breakout_in_words() -> None:
    prompt = build_prompt(_obj_with_breakouts({"A": "up", "B": "down"}))
    assert "A closed above a level" in prompt
    assert "B closed below a level" in prompt


def test_breakout_framing_carries_no_digits() -> None:
    """The framing is injected into the prompt the model answers from, and the
    standing rule is that it never sees a figure it might echo back."""
    prompt = build_prompt(_obj_with_breakouts({"A": "up"}))
    framing = prompt.split("Session data")[0]
    line = next(x for x in framing.splitlines() if x.startswith("Technical context"))
    assert not any(ch.isdigit() for ch in line)


def test_a_quiet_session_gets_no_technical_framing() -> None:
    """Silent by default: no breakout, no clause. §4 still renders its table."""
    assert "Technical context" not in build_prompt(_obj_with_breakouts({}))


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


def test_open_prompt_asks_for_why_keyed_to_premarket_symbols() -> None:
    """M15: §3's pre-market rows get a `why`, the same way §7's attribution
    rows do on the close brief — it's §4/§5/§6 that carry no `why` line."""
    prompt = build_open_prompt(_open_object_with_premarket("SNDK"))
    assert "'SNDK'" in prompt
    assert '"why"' in prompt


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


# --- §2's read and §3's why (M15) ------------------------------------------


def _open_object_with_tape() -> BriefObject:
    """A minimal open brief carrying one §2 overnight-tape row."""
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
                    "id": "overnight_tape",
                    "tier": "full",
                    "note": None,
                    "rows": [
                        {
                            "symbol": "ES",
                            "label": "E-mini S&P futures",
                            "level": 5123.25,
                            "overnight_pct": -0.004,
                            "overnight_abs": None,
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


def _open_object_with_premarket(symbol: str) -> BriefObject:
    """A minimal open brief carrying one §3 pre-market row for ``symbol``."""
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
                    "id": "premarket",
                    "tier": "full",
                    "note": None,
                    "rows": [
                        {
                            "symbol": symbol,
                            "pre_pct": -0.031,
                            "gap_cents": -4820,
                            "premarket_vol_mult": 2.1,
                            "why": None,
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


def test_tape_read_lands_in_the_overnight_section() -> None:
    obj = _open_object_with_tape()
    narrated = narrate_open_and_apply(
        obj, lambda _p: json.dumps({"tape_read": "Risk-off overnight; your semis proxy is soft."})
    )
    section = next(s for s in narrated.sections if s.id.value == "overnight_tape")
    assert section.note == "Risk-off overnight; your semis proxy is soft."


def test_a_tape_read_with_a_digit_is_dropped() -> None:
    """Invariant 2, unchanged: the tables carry every figure."""
    obj = _open_object_with_tape()
    narrated = narrate_open_and_apply(obj, lambda _p: json.dumps({"tape_read": "ES fell 0.4%."}))
    section = next(s for s in narrated.sections if s.id.value == "overnight_tape")
    assert section.note == next(
        s for s in obj.sections if s.id.value == "overnight_tape"
    ).note


def test_why_fills_premarket_rows() -> None:
    obj = _open_object_with_premarket("SNDK")
    narrated = narrate_open_and_apply(
        obj, lambda _p: json.dumps({"why": {"SNDK": "Memory pricing read-through."}})
    )
    row = next(s for s in narrated.sections if s.id.value == "premarket").rows[0]
    assert row.why == "Memory pricing read-through."


def test_parse_drops_a_why_for_an_unlisted_symbol() -> None:
    """Mixes one valid symbol in with the invalid one on purpose: an
    unlisted-only payload would pass even with the parser's `if symbol in
    symbols` filter deleted, because `apply_narration`'s row lookup is keyed
    on the row's own symbol and would find nothing to attach it to either
    way. Pairing it with a real symbol is what makes the filter's drop
    observable — mirrors the close brief's `test_parse_drops_unknown_symbols`."""
    obj = _open_object_with_premarket("SNDK")
    raw = json.dumps({"why": {
        "SNDK": "Memory pricing read-through.",
        "NVDA": "Not in this brief.",
    }})
    narration = parse_narration(raw, obj)
    assert narration.why == {
        "SNDK": "Memory pricing read-through."
    }


def test_narration_stays_non_fatal_for_the_open_brief() -> None:
    """D19 parity: the always-sending brief keeps sending."""
    obj = _open_object_with_tape()

    def boom(_p: str) -> str:
        raise RuntimeError("no key")

    assert narrate_open_and_apply(obj, boom) == obj


# --- C1: an empty §2 must never be overwritten by a hallucinated read ------

_OMITTED_TAPE_NOTE = "No overnight tape captured for this session."


def _open_object_with_empty_tape() -> BriefObject:
    """A minimal open brief with §2 suppressed — no rows, the sentinel note."""
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
                    "id": "overnight_tape",
                    "tier": "suppressed",
                    "note": _OMITTED_TAPE_NOTE,
                    "rows": [],
                }
            ],
            "flags": [],
            "claims": [],
            "resolved_claims": [],
            "suppressed": [],
            "data_quality": {"missing": [], "stale": []},
        }
    )


def test_tape_read_never_overwrites_the_omitted_note() -> None:
    """A `tape_read` returned for an empty §2 (the model was asked anyway, or a
    stale/adversarial narrator response) must leave the honest sentinel note
    intact — the digit guard cannot catch this because the hallucination is
    prose, not a figure."""
    obj = _open_object_with_empty_tape()
    narrated = narrate_open_and_apply(
        obj, lambda _p: json.dumps({"tape_read": "Futures point broadly higher overnight."})
    )
    section = next(s for s in narrated.sections if s.id.value == "overnight_tape")
    assert section.note == _OMITTED_TAPE_NOTE


def test_open_prompt_does_not_ask_for_tape_read_when_section_is_empty() -> None:
    """The model is never invited to narrate absent data (C1's other half)."""
    prompt = build_open_prompt(_open_object_with_empty_tape())
    assert '"tape_read"' not in prompt


def test_open_prompt_asks_for_tape_read_when_section_has_rows() -> None:
    """The normal case still works: a populated §2 gets the ask."""
    prompt = build_open_prompt(_open_object_with_tape())
    assert '"tape_read"' in prompt


# --- News headlines in the open prompt (M16) --------------------------------


def test_open_prompt_carries_headlines_verbatim() -> None:
    obj = _open_obj()
    prompt = build_open_prompt(obj, headlines={"ASTS": ["FCC approves"]})
    assert "FCC approves" in prompt
    assert build_open_prompt(obj) == build_open_prompt(obj, headlines={})
