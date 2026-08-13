"""Stage ④ for the open brief: pure, no DB, no P&L (M14).

The open brief is forward-looking and carries no performance (docs/05), so the
object has **no `book`** and never runs the compute path. §2/§3 are rendered as
an omitted-note until their feeds land in M15, and unlike the close brief there
is no skip gate — "nothing happened overnight" is information.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from contracts.brief import BriefObject, Section
from worker.assemble_open import (
    SectorSetup,
    assemble_open,
    trailing_return,
)
from worker.events_seed import CalendarEvent
from worker.flags import FlagCandidate
from worker.premarket import TapeQuote

_SESSION = date(2026, 8, 13)  # a Thursday
_PRIOR = date(2026, 8, 12)
_USER = "00000000-0000-0000-0000-000000000001"
_GENERATED_AT = datetime(2026, 8, 13, 12, 15, 0, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures" / "open_brief.json"


def _dec(values: list[str]) -> list[Decimal]:
    return [Decimal(v) for v in values]


# --- §5's pure return helper ----------------------------------------------


def test_trailing_return_over_five_sessions() -> None:
    # Six closes → five sessions of return: 110/100 - 1 = +10%.
    closes = _dec(["100", "102", "104", "106", "108", "110"])
    assert trailing_return(closes, 5) == Decimal("0.1")


def test_trailing_return_uses_only_the_last_n_sessions() -> None:
    """A longer history must not change the answer — the window is trailing."""
    short = _dec(["100", "102", "104", "106", "108", "110"])
    long = _dec(["1", "50", *["100", "102", "104", "106", "108", "110"]])
    assert trailing_return(long, 5) == trailing_return(short, 5)


def test_trailing_return_is_none_without_enough_history() -> None:
    assert trailing_return(_dec(["100", "101"]), 5) is None


def test_trailing_return_is_none_on_a_zero_base() -> None:
    assert trailing_return(_dec(["0", "1", "2", "3", "4", "5"]), 5) is None


# --- The object -----------------------------------------------------------


def _events() -> list[CalendarEvent]:
    return [
        CalendarEvent(None, "macro", _SESSION, "CPI (m/m)"),
        CalendarEvent("SNDK", "earnings", _SESSION, "SNDK Q2 earnings"),
        CalendarEvent("ASTS", "ex_div", date(2026, 8, 17), "ASTS ex-dividend"),
    ]


def _sectors() -> list[SectorSetup]:
    return [
        SectorSetup(
            sector_id="s1", name="Semis", benchmark_symbol="SMH",
            ret_5d=Decimal("0.031"), vs_spy_5d=Decimal("0.012"),
        ),
        SectorSetup(
            sector_id="s2", name="Space", benchmark_symbol=None,
            ret_5d=None, vs_spy_5d=None,
        ),
    ]


def _open(**kw: object) -> BriefObject:
    params: dict[str, object] = {
        "events": _events(),
        "sectors": _sectors(),
        "flags": [],
        "holdings": {"SNDK": "owned", "ASTS": "watching"},
        "tape": [],
        "user_id": _USER,
        "session_date": _SESSION,
        "prior_session": _PRIOR,
        "generated_at": _GENERATED_AT,
    }
    params.update(kw)
    return assemble_open(**params)  # type: ignore[arg-type]


def _assemble(**overrides: object) -> BriefObject:
    kwargs: dict[str, object] = {
        "events": [], "sectors": [], "flags": [], "holdings": {},
        "tape": [], "premarket": [], "claims": [],
        "user_id": _USER, "session_date": _SESSION, "prior_session": _PRIOR,
        "generated_at": _GENERATED_AT,
    }
    kwargs.update(overrides)
    return assemble_open(**kwargs)  # type: ignore[arg-type]


def _section(obj: BriefObject, section_id: str) -> Section:
    return next(s for s in obj.sections if s.id.value == section_id)


def test_open_brief_omits_the_book_entirely() -> None:
    """docs/05: no performance, no P&L. Not a zeroed book — absent."""
    obj = _open()
    assert obj.book is None
    assert "book" not in obj.model_dump(mode="json", exclude_none=True)


def test_kind_is_open_and_brief_id_says_so() -> None:
    obj = _open()
    assert obj.kind.value == "open"
    assert obj.brief_id.endswith("-open")


def test_sections_are_the_four_m14_builds_plus_two_omitted() -> None:
    ids = [s.id.value for s in _open().sections]
    assert ids == [
        "overnight_tape",
        "premarket",
        "calendar",
        "sector_setup",
        "exposure_check",
    ]


def test_overnight_tape_and_premarket_are_suppressed_with_a_note() -> None:
    """M14 ships §1/§4/§5/§6; §2/§3 need feeds that don't exist yet, and say so
    rather than silently vanishing."""
    obj = _open()
    for section_id in ("overnight_tape", "premarket"):
        section = next(s for s in obj.sections if s.id.value == section_id)
        assert section.tier.value == "suppressed"
        assert section.rows == []
        assert section.note is not None and section.note.strip()


def test_calendar_tags_holdings_watchlist_and_macro() -> None:
    calendar = next(s for s in _open().sections if s.id.value == "calendar")
    tags = {r.label: r.tag.value for r in calendar.rows if r.tag is not None}
    assert tags == {
        "CPI (m/m)": "macro",
        "SNDK Q2 earnings": "holding",
        "ASTS ex-dividend": "watchlist",
    }


def test_macro_calendar_row_has_no_symbol() -> None:
    calendar = next(s for s in _open().sections if s.id.value == "calendar")
    macro = next(r for r in calendar.rows if r.label == "CPI (m/m)")
    assert macro.symbol is None


def test_calendar_rows_are_ordered_by_date() -> None:
    calendar = next(s for s in _open().sections if s.id.value == "calendar")
    dates = [r.occurs_at for r in calendar.rows]
    # Every calendar row is dated — a row without a date has nothing to be "on
    # the clock" for. Asserting it also makes the ordering check meaningful.
    assert all(d is not None for d in dates)
    assert dates == sorted(d for d in dates if d is not None)


def test_sector_setup_carries_returns_and_a_null_premarket() -> None:
    setup = next(s for s in _open().sections if s.id.value == "sector_setup")
    semis = next(r for r in setup.rows if r.name == "Semis")
    assert semis.benchmark_symbol == "SMH"
    assert semis.ret_5d == 0.031
    assert semis.vs_spy_5d == 0.012
    assert semis.premarket is None  # the M15 column


def test_a_sector_without_a_benchmark_still_gets_a_row() -> None:
    """The book may not have a benchmark set for every sector; the section
    should degrade to a named row with null figures, not drop the sector."""
    setup = next(s for s in _open().sections if s.id.value == "sector_setup")
    space = next(r for r in setup.rows if r.name == "Space")
    assert space.benchmark_symbol is None
    assert space.ret_5d is None


def test_exposure_check_carries_the_flags() -> None:
    flags = [FlagCandidate("correlation", "warn", None, None, 0.81, "corr_20d_high")]
    obj = _open(flags=flags)

    assert [f.text_key for f in obj.flags] == ["corr_20d_high"]
    section = next(s for s in obj.sections if s.id.value == "exposure_check")
    assert section.tier.value == "full"


def test_exposure_check_is_suppressed_when_nothing_fires() -> None:
    """docs/05: silent by default."""
    section = next(s for s in _open().sections if s.id.value == "exposure_check")
    assert section.tier.value == "suppressed"


def test_emits_and_resolves_no_claims() -> None:
    """M14 resolution stays in the close brief: grading at 08:15 would consume
    the due claims before the close brief's §7 could report them."""
    obj = _open()
    assert obj.claims == []
    assert obj.resolved_claims == []


def test_subject_names_the_session_and_carries_no_pnl() -> None:
    subject = _open().subject
    assert subject.startswith("Open · Thu Aug 13")
    assert "$" not in subject and "%" not in subject


def test_a_completely_empty_day_still_produces_a_valid_brief() -> None:
    """DoD 2: the open brief always sends. No events, no sectors, no flags."""
    obj = _open(events=[], sectors=[], flags=[], holdings={})
    assert obj.kind.value == "open"
    calendar = next(s for s in obj.sections if s.id.value == "calendar")
    assert calendar.tier.value == "suppressed"
    assert calendar.note is not None  # says the day is clear, rather than vanishing


def test_matches_frozen_fixture() -> None:
    got = _open().model_dump(mode="json")
    assert got == json.loads(_FIXTURE.read_text())


# --- §2 overnight tape (M15) ------------------------------------------------


def _tape() -> list[TapeQuote]:
    return [
        TapeQuote("ES=F", "ES futures", Decimal("5612.25"), Decimal("5635.25")),
        TapeQuote("^TNX", "10Y", Decimal("4.28"), Decimal("4.25")),
    ]


def test_overnight_tape_carries_level_and_change() -> None:
    obj = _assemble(tape=_tape())
    section = _section(obj, "overnight_tape")
    assert section.tier.value == "full"
    es, tnx = section.rows
    assert es.label == "ES futures"
    assert es.level == 5612.25
    assert es.overnight_pct is not None and es.overnight_pct < 0
    assert es.overnight_abs == -23.0


def test_level_quoted_symbols_report_no_percent() -> None:
    """A percent change on a yield reads as noise; the absolute move is the
    figure (+3bp), so §2 leaves overnight_pct null for 10Y and VIX."""
    _es, tnx = _section(_assemble(tape=_tape()), "overnight_tape").rows
    assert tnx.overnight_pct is None
    assert tnx.overnight_abs is not None
    assert round(tnx.overnight_abs, 4) == 0.03


def test_an_empty_tape_still_says_so() -> None:
    """No feed, no silence: the M5 precedent — a short brief names what it left
    out rather than quietly shrinking."""
    section = _section(_assemble(tape=[]), "overnight_tape")
    assert section.tier.value == "suppressed"
    assert section.note is not None
