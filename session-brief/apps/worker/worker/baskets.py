"""Pure basket math for attribution (M11): point-in-time membership,
equal-weight basket return, and analytic leave-one-out. Float, off the money
path (like flags.mean_pairwise_corr); callers quantize to Decimal at storage.
No network, clock, or DB in the pure section."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class Member:
    symbol: str
    effective_from: date
    effective_to: date | None  # None = still effective


@dataclass(frozen=True)
class BasketReturn:
    ret: float
    n_members: int


def members_on(members: list[Member], on: date) -> list[str]:
    """Symbols whose [effective_from, effective_to) window contains `on`.
    History is never recomputed against current membership (docs/03)."""
    live = [
        m.symbol for m in members
        if m.effective_from <= on and (m.effective_to is None or on < m.effective_to)
    ]
    return sorted(live)


def equal_weight_return(returns: dict[str, float]) -> BasketReturn:
    """Equal-weight mean of member returns. `returns` must already be filtered
    to the point-in-time membership for the day."""
    n = len(returns)
    if n == 0:
        raise ValueError("empty basket: no members with a return for this day")
    return BasketReturn(ret=sum(returns.values()) / n, n_members=n)


def leave_one_out(full_ret: float, n: int, r_x: float) -> float:
    """The equal-weight basket return with symbol X removed, in O(1):
    r_LOO(X) = (n*r_full - r_X) / (n-1). A name is never regressed on a basket
    containing itself."""
    if n < 2:
        raise ValueError("leave-one-out is undefined for a basket of size < 2")
    return (n * full_ret - r_x) / (n - 1)


BASKET_CAP = 0.25
MIN_DOLLAR_VOLUME = 1_000_000.0


def screen_and_cap(
    liquidity: dict[str, float], *, min_dollar_volume: float, cap: float
) -> dict[str, float]:
    """Dollar-volume-weighted, liquidity-screened, capped weights summing to 1.
    Names below the dollar-volume floor are dropped; survivors are weighted
    proportional to dollar volume; no name exceeds `cap`; excess from capped names
    redistributes to the uncapped survivors (proportional to their base weight),
    iterated to a fixed point. Equal weighting would make the cap vestigial — the
    cap is what stops one mega-cap from becoming the basket, and what breaks the
    analytic leave-one-out. If the cap is infeasible for the survivor count
    (`n * cap < 1`, e.g. fewer than 4 names at a 0.25 cap), the cap cannot hold
    while summing to 1, so we fall back to equal weight — the least-concentrated
    valid distribution."""
    survivors = sorted(s for s, dv in liquidity.items() if dv >= min_dollar_volume)
    if not survivors:
        return {}
    n = len(survivors)
    if cap < 1.0 and n * cap < 1.0:
        return {s: 1.0 / n for s in survivors}
    dv_total = sum(liquidity[s] for s in survivors)
    base = {s: liquidity[s] / dv_total for s in survivors}
    if cap >= 1.0:
        return base

    weights = dict(base)
    capped: set[str] = set()
    for _ in range(len(survivors)):
        over = [s for s in survivors if s not in capped and weights[s] > cap + 1e-12]
        if not over:
            break
        for s in over:
            weights[s] = cap
            capped.add(s)
        uncapped = [s for s in survivors if s not in capped]
        budget = 1.0 - cap * len(capped)
        base_sum = sum(base[s] for s in uncapped)
        if not uncapped or budget <= 0 or base_sum == 0:
            break
        for s in uncapped:
            weights[s] = budget * base[s] / base_sum
    return weights


def weighted_return(returns: dict[str, float], weights: dict[str, float]) -> BasketReturn:
    """Weighted mean of member returns over the intersection of `returns` and
    `weights`, renormalized so the used weights sum to 1."""
    used = {s: weights[s] for s in weights if s in returns}
    total = sum(used.values())
    if total == 0:
        raise ValueError("empty basket: no weighted members with a return")
    ret = sum(returns[s] * used[s] for s in used) / total
    return BasketReturn(ret=ret, n_members=len(used))


def loo_weighted_return(
    returns: dict[str, float], liquidity: dict[str, float], *,
    min_dollar_volume: float, cap: float, excluded: str,
) -> BasketReturn:
    """Leave-one-out basket return under capping/screening: remove `excluded`,
    then re-screen and re-cap the survivors (the freed weight redistributes and
    the cap can re-bind). This is why analytic O(1) LOO no longer holds (spec)."""
    liq = {s: dv for s, dv in liquidity.items() if s != excluded}
    weights = screen_and_cap(liq, min_dollar_volume=min_dollar_volume, cap=cap)
    return weighted_return(returns, weights)


# --- Database layer -------------------------------------------------------

_READ_MEMBERS = text("""
    SELECT theme_id::text AS theme_id, symbol, is_primary,
           effective_from, effective_to
    FROM theme_members
""")

_UPSERT_BASKET = text("""
    INSERT INTO basket_returns
        (theme_id, trade_date, model_version, ret, n_members, synthetic, revised, weights)
    VALUES (:theme_id, :trade_date, :model_version, :ret, :n_members, :synthetic, :revised,
            CAST(:weights AS jsonb))
    ON CONFLICT (theme_id, trade_date, model_version) DO UPDATE
        SET ret = EXCLUDED.ret, n_members = EXCLUDED.n_members,
            synthetic = EXCLUDED.synthetic, revised = EXCLUDED.revised,
            weights = EXCLUDED.weights
""")


def read_theme_members(conn: Connection) -> dict[str, list[Member]]:
    """All theme memberships, grouped by theme_id (as a str uuid)."""
    by_theme: dict[str, list[Member]] = {}
    for row in conn.execute(_READ_MEMBERS).mappings():
        by_theme.setdefault(row["theme_id"], []).append(
            Member(row["symbol"], row["effective_from"], row["effective_to"])
        )
    return by_theme


def primary_theme_of(
    by_theme: dict[str, list[Member]], symbol: str, on: date
) -> str | None:
    """The theme_id for which `symbol` is a point-in-time is_primary member on
    `on`, or None. Exactly one primary per symbol per date (seed invariant)."""
    for theme_id, members in by_theme.items():
        for m in members:
            if (m.symbol == symbol and m.effective_from <= on
                    and (m.effective_to is None or on < m.effective_to)):
                return theme_id
    return None


def upsert_basket_return(
    conn: Connection, theme_id: str, trade_date: date, model_version: int,
    br: BasketReturn, *, synthetic: bool, revised: bool,
    weights: dict[str, float] | None = None,
) -> None:
    import json
    conn.execute(_UPSERT_BASKET, {
        "theme_id": theme_id, "trade_date": trade_date, "model_version": model_version,
        "ret": br.ret, "n_members": br.n_members, "synthetic": synthetic, "revised": revised,
        "weights": json.dumps(weights) if weights is not None else None,
    })


_UPSERT_BASKET_LOO = text("""
    INSERT INTO basket_loo_returns
        (theme_id, excluded_symbol, trade_date, model_version, ret, n_members)
    VALUES (:theme_id, :excluded_symbol, :trade_date, :model_version, :ret, :n_members)
    ON CONFLICT (theme_id, excluded_symbol, trade_date, model_version) DO UPDATE
        SET ret = EXCLUDED.ret, n_members = EXCLUDED.n_members
""")


def upsert_basket_loo_return(
    conn: Connection, theme_id: str, excluded_symbol: str, trade_date: date,
    model_version: int, br: BasketReturn,
) -> None:
    conn.execute(_UPSERT_BASKET_LOO, {
        "theme_id": theme_id, "excluded_symbol": excluded_symbol,
        "trade_date": trade_date, "model_version": model_version,
        "ret": br.ret, "n_members": br.n_members,
    })
