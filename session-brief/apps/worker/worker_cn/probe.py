"""``tiingo-cn-probe`` (CN-M3, Task 10): answers CN-Q1/CN-Q2/CN-Q4 in
``cn/docs/open-questions.md`` by trying candidate Tiingo ticker formats
against real CN symbols. Read-only (no database writes, no raw_payloads
capture), always exits 0 — the CLI wrapper (``worker.cli``) mirrors
``_fdn_probe``'s ✓/✗-line style; a human reads the output and decides whether
``CN_TIINGO_FORMATS`` needs an edit. CN business logic (candidate formats,
XSHG calendar comparisons) lives here, not in ``worker/cli.py``, per
``cn/README.md``'s separation rule — the CLI only routes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx

from worker.providers.tiingo import TiingoProvider
from worker_cn.calendar import CN

DEFAULT_SYMBOLS: list[str] = ["600519.SS", "300750.SZ", "000300.SS"]

# Suffix -> Tiingo's exchange code, for building the "-SHG"/"-SHE" and
# "-SS"/"-SZ" candidates below.
_EXCHANGE_CODE = {".SS": "SHG", ".SZ": "SHE"}

# Errors a bad candidate ticker can raise: a non-2xx status, a connection
# failure, or `_fetch_daily_bars`'s own "non-list body" ValueError.
_PROBE_ERRORS = (httpx.HTTPError, ValueError)


def _candidates(symbol: str) -> list[str]:
    """The candidate vendor ticker strings for one internal CN symbol — at
    minimum the CN_TIINGO_FORMATS guess (`{code}-SHG`/`{code}-SHE`), the
    suffixed symbol passed straight through (`{code}.SS`/`{code}.SZ`), the
    bare numeric code, and the `-SS`/`-SZ` dash variant (CN-Q1)."""
    symbol_upper = symbol.upper()
    for suffix, exch in _EXCHANGE_CODE.items():
        if symbol_upper.endswith(suffix):
            code = symbol[: -len(suffix)]
            alt = suffix[1:]  # "SS" or "SZ"
            return [f"{code}-{exch}", f"{code}{suffix}", code, f"{code}-{alt}"]
    return [symbol]  # not a recognized CN suffix; try it as-is


def _error_label(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _latest_xshg_session(today: date) -> date:
    """The most recent XSHG session on or before ``today`` — the CN-Q4
    same-day-latency reference point."""
    return today if CN.is_session(today) else CN.previous_session(today)


def _window_start(latest_session: date, sessions: int = 10) -> date:
    """Walks back ``sessions`` XSHG sessions (inclusive of ``latest_session``)
    to get a ~10-session probe window's start."""
    d = latest_session
    for _ in range(sessions - 1):
        d = CN.previous_session(d)
    return d


def tiingo_cn_probe(
    provider: TiingoProvider, *, symbols: list[str], today: date | None = None
) -> None:
    """Prints one ✓/✗ line per symbol. ``today`` is injectable so tests never
    depend on wall-clock time; the CLI leaves it unset (real "today"). Never
    raises on a vendor error — only a caller bug (a bad ``provider``) would."""
    now = datetime.now(UTC)
    local_today = today if today is not None else CN.local_date(now)
    latest_session = _latest_xshg_session(local_today)
    start = _window_start(latest_session)

    print(
        f"tiingo-cn-probe: now={now.isoformat()} "
        f"latest XSHG session={latest_session.isoformat()} "
        f"window={start.isoformat()}..{latest_session.isoformat()}"
    )
    print(
        "tiingo-cn-probe: CN-Q4's same-day-timing half is only definitive from "
        "a run at ~15:20 CST on a session day — this run's same-day verdict "
        "below reflects whatever wall-clock time it actually executed at."
    )

    for symbol in symbols:
        candidates = _candidates(symbol)
        resolved: tuple[str, list[dict[str, Any]]] | None = None
        tried: list[str] = []
        for candidate in candidates:
            try:
                records = provider._fetch_daily_bars(candidate, start, latest_session)
            except _PROBE_ERRORS as exc:
                tried.append(f"{candidate} ({_error_label(exc)})")
                continue
            if not records:
                tried.append(f"{candidate} (0 records)")
                continue
            resolved = (candidate, records)
            break

        if resolved is None:
            print(f"✗ {symbol}: no candidate format resolved — tried {', '.join(tried)}")
            continue

        fmt, records = resolved
        has_adj = all("adjClose" in r for r in records)
        diverges = any("adjClose" in r and r["adjClose"] != r["close"] for r in records)
        bar_dates = sorted(str(r.get("date", ""))[:10] for r in records)
        latest_bar = bar_dates[-1] if bar_dates else "n/a"
        same_day = latest_bar == latest_session.isoformat()
        print(
            f"✓ {symbol}: format={fmt!r}, {len(records)} record(s), "
            f"adjClose present={has_adj}, adjClose ever != close={diverges}, "
            f"latest bar={latest_bar} vs latest XSHG session={latest_session.isoformat()} "
            f"({'same-day' if same_day else 'BEHIND'})"
        )
