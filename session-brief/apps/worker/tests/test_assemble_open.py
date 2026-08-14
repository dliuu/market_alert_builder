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
from worker.claims import emit_premarket_gap
from worker.events_seed import CalendarEvent
from worker.flags import FlagCandidate
from worker.premarket import PremarketQuote, TapeQuote

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


def test_a_failed_calendar_endpoint_lands_in_data_quality_missing() -> None:
    """Fix A (M16 final review): a partial calendar fetch still clears its whole
    window (events_fdn.py's `_DELETE_WINDOW`), so §4 alone can't tell a quiet
    week from a broken feed. `missing` is how `ingest_events_for_session`'s
    failed-endpoint names (see `test_events_fdn.py`) are meant to surface —
    this checks the BriefObject end of that wiring."""
    obj = _open(missing=["calendar.economic"])
    assert obj.data_quality.missing == ["calendar.economic"]


def test_matches_frozen_fixture() -> None:
    """M15: the snapshot now carries a populated §2 and §3 — one tape read, one
    name gapping pre-market (SNDK, clears the threshold) and one flat (RKLB,
    rolls up into suppressed[]) — plus the horizon-0 claim SNDK's gap emits."""
    premarket = [
        _pm("SNDK", "49.26", "47.32"),
        _pm("RKLB", "24.98", "25.00"),
    ]
    got = _open(
        tape=_tape(),
        premarket=premarket,
        claims=emit_premarket_gap(premarket),
    ).model_dump(mode="json")
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


# --- §3 pre-market names, §5's premarket column, §1 salience (M15) ---------


def _pm(
    symbol: str, last: str, prev: str, v: int = 100_000, typical: str = "50000"
) -> PremarketQuote:
    return PremarketQuote(symbol, Decimal(last), v, Decimal(prev), Decimal(typical))


def test_premarket_lists_only_names_over_the_threshold() -> None:
    obj = _assemble(premarket=[
        _pm("SNDK", "49.26", "47.32"),   # +4.1%
        _pm("RKLB", "24.98", "25.00"),   # -0.1%
    ])
    section = _section(obj, "premarket")
    assert [r.symbol for r in section.rows] == ["SNDK"]


def test_a_row_carries_dollars_and_a_volume_multiple() -> None:
    row = _section(_assemble(premarket=[_pm("SNDK", "49.26", "47.32", v=150_000)]),
                   "premarket").rows[0]
    assert row.gap_cents == 194
    assert row.premarket_vol_mult == 3.0
    assert row.rvol is None  # never the 30-day RVOL (D3, docs/05)
    assert row.why is None   # narration fills it, stage ⑤


def test_names_under_the_threshold_roll_up() -> None:
    """The suppression principle (M5): the brief names what it skipped rather
    than pretending the book is two stocks."""
    obj = _assemble(premarket=[
        _pm("SNDK", "49.26", "47.32"),
        _pm("RKLB", "24.98", "25.00"),
        _pm("MU", "100.10", "100.00"),
    ])
    assert set(obj.suppressed) == {"RKLB", "MU"}
    assert _section(obj, "premarket").note is not None


def test_a_quiet_premarket_is_a_roll_up_line_not_an_empty_table() -> None:
    obj = _assemble(premarket=[_pm("RKLB", "24.98", "25.00")])
    section = _section(obj, "premarket")
    assert section.rows == []
    assert section.tier.value == "suppressed"
    assert "RKLB" in (section.note or "")


def test_the_biggest_gap_leads() -> None:
    """§1 salience: `one_thing` leads on the largest pre-market gap, so the row
    order is the salience order the narration prompt reads."""
    obj = _assemble(premarket=[
        _pm("AAOI", "18.30", "18.06"),   # +1.3%
        _pm("ASTS", "34.20", "36.08"),   # -5.2%
        _pm("SNDK", "49.26", "47.32"),   # +4.1%
    ])
    assert [r.symbol for r in _section(obj, "premarket").rows] == ["ASTS", "SNDK", "AAOI"]


def test_sector_setup_carries_the_premarket_column() -> None:
    sector = SectorSetup(
        sector_id="s1", name="Semis", benchmark_symbol="SMH",
        ret_5d=Decimal("0.02"), vs_spy_5d=Decimal("0.01"), premarket=Decimal("0.004"),
    )
    row = _section(_assemble(sectors=[sector]), "sector_setup").rows[0]
    assert row.premarket == 0.004


# --- News: the §3 has_news gate (M16) ---------------------------------------


def test_a_sub_threshold_name_with_news_still_gets_a_row_but_no_claim() -> None:
    """News is a visibility gate and a narration input only — it must not
    create a claim, because news presence is not a directional call."""
    quote = _pm("ZNEWS", "100.50", "100.00")  # +0.5%, under PREMARKET_THRESHOLD
    obj = _assemble(
        premarket=[quote],
        holdings={"ZNEWS": "owned"},
        claims=emit_premarket_gap([quote]),
        news={"ZNEWS": ["Zed lands a contract"]},
    )
    pre = _section(obj, "premarket")
    assert [r.symbol for r in pre.rows] == ["ZNEWS"]  # 0.5% gap, shown via news
    assert obj.claims == []                            # news is not a directional call
