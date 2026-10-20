"""Option premium and volatility (M22): the strip, IV30 and realized vol.

Three jobs. ``select_strip`` reads a chain into twelve call cells (four tenors
by three strike rules). ``iv30_from_chain`` builds a constant-maturity 30-day
implied vol from the same chain, and ``realized_vol`` its backward-looking
counterpart from closes. Percentile standing of the result reuses the M20 rule
in ``worker/oscillators.py``: ranked against the symbol's own trailing year.

The pure half comes first and touches no database; ``Decimal`` throughout,
under the same explicit 28-digit context as ``oscillators``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from worker.constants import (
    IV30_NEAR_MIN_DTE,
    IV30_PARITY_TOL_FRACTION,
    IV30_PARITY_TOL_POINTS,
    IV30_STRIKES_EACH_SIDE,
    IV30_TARGET_DAYS,
    MIN_DTE,
    STRIKE_RULES,
    TARGET_MISS_TOL,
    TENORS,
)
from worker.options import Contract

PREC = 28
_TRADING_DAYS = Decimal(252)
_HUNDRED = Decimal(100)

# --- pure --------------------------------------------------------------------


@dataclass(frozen=True)
class StripCell:
    tenor: int
    rule: str
    target: Decimal  # multiple of spot the rule asked for
    contract: Contract | None
    dte: int | None
    moneyness: Decimal | None  # strike / spot - 1
    mid: Decimal | None
    premium_pct: Decimal | None  # mid / spot
    time_value_pct: Decimal | None  # (mid - intrinsic) / spot
    target_miss: bool
    tenor_collapsed: bool


def select_strip(contracts: list[Contract], spot: Decimal, session_date: date) -> list[StripCell]:
    """Twelve cells per name: for each tenor the listed expiry nearest it (tie
    → nearer), and at that expiry the two-sided call nearest each target
    strike (tie → lower). A tenor that resolves to an expiry an earlier tenor
    already took is marked ``tenor_collapsed`` and still reported."""
    calls = [
        c
        for c in contracts
        if c.right == "C" and c.two_sided and (c.expiry - session_date).days >= MIN_DTE
    ]
    expiries = sorted({c.expiry for c in calls})
    used: set[date] = set()
    cells: list[StripCell] = []
    for tenor in TENORS:
        if not expiries:
            cells.extend(
                StripCell(tenor, rule, target, None, None, None, None, None, None, False, False)
                for rule, target in STRIKE_RULES
            )
            continue
        expiry = min(
            expiries,
            key=lambda e: (abs((e - session_date).days - tenor), (e - session_date).days),
        )
        collapsed = expiry in used
        used.add(expiry)
        dte = (expiry - session_date).days
        at_expiry = [c for c in calls if c.expiry == expiry]
        for rule, target in STRIKE_RULES:
            want = spot * target
            chosen = min(at_expiry, key=lambda c: (abs(c.strike - want), c.strike))
            moneyness = chosen.strike / spot - 1
            intrinsic = max(spot - chosen.strike, Decimal(0))
            cells.append(
                StripCell(
                    tenor=tenor,
                    rule=rule,
                    target=target,
                    contract=chosen,
                    dte=dte,
                    moneyness=moneyness,
                    mid=chosen.mid,
                    premium_pct=chosen.mid / spot,
                    time_value_pct=(chosen.mid - intrinsic) / spot,
                    target_miss=abs(moneyness - (target - 1)) > TARGET_MISS_TOL,
                    tenor_collapsed=collapsed,
                )
            )
    return cells


@dataclass(frozen=True)
class ExpiryIv:
    expiry: date
    dte: int
    iv: Decimal  # vol points, mean over the contributing contracts
    n: int


@dataclass(frozen=True)
class Iv30:
    iv30: Decimal | None  # vol points
    n: int
    near: ExpiryIv | None
    far: ExpiryIv | None
    dropped: tuple[str, ...]  # "YYYY-MM-DD strike" pairs the parity check removed
    contributors: tuple[str, ...]  # symbols of the contracts whose iv fed the number


def iv30_from_chain(contracts: list[Contract], spot: Decimal, session_date: date) -> Iv30:
    eligible = [
        c
        for c in contracts
        if c.two_sided and c.iv is not None and (c.expiry - session_date).days >= IV30_NEAR_MIN_DTE
    ]
    expiries = sorted({c.expiry for c in eligible})
    if not expiries:
        return Iv30(None, 0, None, None, (), ())

    def dte(e: date) -> int:
        return (e - session_date).days

    at_or_below = [e for e in expiries if dte(e) <= IV30_TARGET_DAYS]
    above = [e for e in expiries if dte(e) > IV30_TARGET_DAYS]
    near_expiry = max(at_or_below) if at_or_below else None
    far_expiry = min(above) if above else None
    dropped: list[str] = []
    contributors: list[str] = []

    def expiry_iv(e: date) -> ExpiryIv | None:
        at_expiry = [c for c in eligible if c.expiry == e]
        strikes = sorted({c.strike for c in at_expiry})
        below = [k for k in strikes if k <= spot][-IV30_STRIKES_EACH_SIDE:]
        above_spot = [k for k in strikes if k > spot][:IV30_STRIKES_EACH_SIDE]
        ivs: list[Decimal] = []
        for k in below + above_spot:
            call = next((c for c in at_expiry if c.strike == k and c.right == "C"), None)
            put = next((c for c in at_expiry if c.strike == k and c.right == "P"), None)
            if call is not None and put is not None:
                assert call.iv is not None and put.iv is not None  # eligible ⇒ iv present
                tolerance = max(
                    IV30_PARITY_TOL_POINTS, IV30_PARITY_TOL_FRACTION * (call.iv + put.iv) / 2
                )
                if abs(call.iv - put.iv) > tolerance:
                    dropped.append(f"{e.isoformat()} {k}")
                    continue
            for c in (call, put):
                if c is not None and c.iv is not None:
                    ivs.append(c.iv)
                    contributors.append(c.symbol)
        if not ivs:
            return None
        return ExpiryIv(expiry=e, dte=dte(e), iv=sum(ivs, Decimal(0)) / len(ivs), n=len(ivs))

    near = expiry_iv(near_expiry) if near_expiry is not None else None
    far = expiry_iv(far_expiry) if far_expiry is not None else None
    iv30: Decimal | None
    if near is not None and far is not None and near.dte != IV30_TARGET_DAYS:
        # Linear in total variance (iv² · T), the standard constant-maturity
        # construction; a straight line in iv would understate the near leg.
        var_near = near.iv * near.iv * near.dte
        var_far = far.iv * far.iv * far.dte
        weight = Decimal(IV30_TARGET_DAYS - near.dte) / Decimal(far.dte - near.dte)
        var_30 = var_near + (var_far - var_near) * weight
        iv30 = (var_30 / IV30_TARGET_DAYS).sqrt()
    elif near is not None:
        iv30 = near.iv
    elif far is not None:
        iv30 = far.iv
    else:
        iv30 = None
    n = (near.n if near else 0) + (far.n if far else 0)
    return Iv30(
        iv30=iv30,
        n=n,
        near=near,
        far=far,
        dropped=tuple(dropped),
        contributors=tuple(contributors),
    )


def selected_contracts(
    contracts: list[Contract], spot: Decimal, session_date: date
) -> list[Contract]:
    """Every contract the strip or IV30 consulted, deduped, ordered by symbol."""
    wanted = set(iv30_from_chain(contracts, spot, session_date).contributors)
    wanted |= {
        cell.contract.symbol
        for cell in select_strip(contracts, spot, session_date)
        if cell.contract is not None
    }
    by_symbol = {c.symbol: c for c in contracts if c.symbol in wanted}
    return [by_symbol[s] for s in sorted(by_symbol)]


def realized_vol(closes: Sequence[Decimal], window: int) -> Decimal | None:
    """Annualised sample stdev of the ``window`` daily log returns ending at the
    prior session, in vol points. ``closes`` run oldest → newest with today
    last; today's close is excluded (it may be an intraday print), so fewer
    than ``window + 2`` closes yields ``None``."""
    if len(closes) < window + 2:
        return None
    used = closes[-(window + 2) : -1]
    with localcontext() as ctx:
        ctx.prec = PREC
        returns = [(b / a).ln() for a, b in zip(used, used[1:], strict=False)]
        mean = sum(returns, Decimal(0)) / window
        variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (window - 1)
        return variance.sqrt() * _TRADING_DAYS.sqrt() * _HUNDRED


def term_slope(cells: list[StripCell]) -> Decimal | None:
    """7-day ATM IV minus 30-day ATM IV; ``None`` when either is missing or the
    two tenors resolved to the same expiry."""
    by_key = {(c.tenor, c.rule): c for c in cells}
    short, long = by_key.get((7, "atm")), by_key.get((30, "atm"))
    if short is None or long is None or short.contract is None or long.contract is None:
        return None
    if short.dte == long.dte:
        return None
    if short.contract.iv is None or long.contract.iv is None:
        return None
    return short.contract.iv - long.contract.iv
