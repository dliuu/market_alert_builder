"""CN open brief assembly (CN-M2): pure, no DB, no P&L.

Mirrors ``tests/test_assemble_open.py``'s pattern — hand-built inputs fed
straight into the pure ``assemble_cn_open()``, snapshotted against a frozen
fixture (docs/04). Unlike the US open brief this carries only three sections
(no premarket, no exposure_check) and no claims, since the CN book's §2 is
built from already-cached US-session bars rather than a pre-market feed."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from contracts.brief import BriefObject
from worker.assemble_open import SectorSetup
from worker.assemble_shared import to_contract_json
from worker.events_seed import CalendarEvent
from worker.premarket import TapeQuote
from worker_cn.assemble import STALE_CN_BARS_SYNTHETIC, assemble_cn_open

_SESSION = date(2026, 8, 14)  # a Friday (XSHG session)
_USER = "00000000-0000-0000-0000-0000000000c2"
_GENERATED_AT = datetime(2026, 8, 14, 1, 10, 0, tzinfo=UTC)  # 09:10 CST
_FIXTURE = Path(__file__).parent / "fixtures" / "cn_open_brief.json"


def _events() -> list[CalendarEvent]:
    return [
        CalendarEvent("600519.SS", "earnings", _SESSION, "600519.SS Q2 earnings"),
        CalendarEvent("300750.SZ", "ex_div", date(2026, 8, 17), "300750.SZ ex-dividend"),
    ]


def _sectors() -> list[SectorSetup]:
    return [
        SectorSetup(
            sector_id="s1", name="Consumer", benchmark_symbol="512690.SS",
            ret_5d=Decimal("0.021"), vs_spy_5d=Decimal("0.009"),
        ),
        SectorSetup(
            sector_id="s2", name="EV", benchmark_symbol=None,
            ret_5d=None, vs_spy_5d=None,
        ),
    ]


def _tape() -> list[TapeQuote]:
    return [
        TapeQuote("SPY", "SPY", Decimal("560.12"), Decimal("558.00")),
        TapeQuote("SMH", "SMH", Decimal("240.00"), Decimal("242.40")),
    ]


def _open(**kw: object) -> BriefObject:
    params: dict[str, object] = {
        "events": _events(),
        "sectors": _sectors(),
        "tape": _tape(),
        "holdings": {"600519.SS": "owned", "300750.SZ": "watching"},
        "user_id": _USER,
        "session_date": _SESSION,
        "generated_at": _GENERATED_AT,
    }
    params.update(kw)
    return assemble_cn_open(**params)  # type: ignore[arg-type]


def test_kind_currency_and_brief_id() -> None:
    obj = _open()
    assert obj.kind.value == "open_cn"
    assert obj.brief_id.endswith("-open_cn")
    assert to_contract_json(obj)["currency"] == "CNY"


def test_subject_carries_cn_open_label_and_no_pnl() -> None:
    subject = _open().subject
    assert subject.startswith("CN Open · Fri Aug 14")
    assert "$" not in subject and "¥" not in subject and "%" not in subject


def test_open_brief_omits_the_book_entirely() -> None:
    obj = _open()
    assert obj.book is None
    assert "book" not in obj.model_dump(mode="json", exclude_none=True)


def test_sections_are_exactly_the_three_no_premarket_no_exposure() -> None:
    ids = [s.id.value for s in _open().sections]
    assert ids == ["overnight_tape", "calendar", "sector_setup"]


def test_no_claims_no_flags() -> None:
    obj = _open()
    assert obj.claims == []
    assert obj.resolved_claims == []
    assert obj.flags == []


def test_overnight_tape_reads_real_us_closes_plainly_labeled() -> None:
    section = next(s for s in _open().sections if s.id.value == "overnight_tape")
    assert section.tier.value == "full"
    spy = next(r for r in section.rows if r.symbol == "SPY")
    assert spy.label == "SPY"
    assert spy.level == 560.12


def test_calendar_rows_come_from_the_cn_book() -> None:
    section = next(s for s in _open().sections if s.id.value == "calendar")
    symbols = {r.symbol for r in section.rows}
    assert symbols == {"600519.SS", "300750.SZ"}


def test_sector_setup_carries_the_cn_returns() -> None:
    setup = next(s for s in _open().sections if s.id.value == "sector_setup")
    consumer = next(r for r in setup.rows if r.name == "Consumer")
    assert consumer.benchmark_symbol == "512690.SS"
    assert consumer.ret_5d == 0.021
    assert consumer.vs_spy_5d == 0.009


def test_synthetic_cn_bars_marker_is_stale_by_default() -> None:
    """CN-M1 default: `CN_BARS_LIVE` unset -> `cn_bars_are_synthetic()` True."""
    obj = _open()
    assert STALE_CN_BARS_SYNTHETIC in obj.data_quality.stale


def test_matches_frozen_fixture() -> None:
    got = to_contract_json(_open())
    assert got == json.loads(_FIXTURE.read_text())
