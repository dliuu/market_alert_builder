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
