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


# --- Database layer -------------------------------------------------------

_READ_MEMBERS = text("""
    SELECT theme_id::text AS theme_id, symbol, is_primary,
           effective_from, effective_to
    FROM theme_members
""")

_UPSERT_BASKET = text("""
    INSERT INTO basket_returns
        (theme_id, trade_date, model_version, ret, n_members, synthetic, revised)
    VALUES (:theme_id, :trade_date, :model_version, :ret, :n_members, :synthetic, :revised)
    ON CONFLICT (theme_id, trade_date, model_version) DO UPDATE
        SET ret = EXCLUDED.ret, n_members = EXCLUDED.n_members,
            synthetic = EXCLUDED.synthetic, revised = EXCLUDED.revised
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
) -> None:
    conn.execute(_UPSERT_BASKET, {
        "theme_id": theme_id, "trade_date": trade_date, "model_version": model_version,
        "ret": br.ret, "n_members": br.n_members, "synthetic": synthetic, "revised": revised,
    })
