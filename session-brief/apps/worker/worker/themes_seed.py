"""Seeded reference themes (M11). Manual, versioned curation via
effective_from/effective_to — the M7 fundamentals/events seeding pattern, not
an ingest. Idempotent: keyed on themes.key and (theme_id, symbol)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

# key -> (display name, sort_order, [member symbols]). Each symbol belongs to
# exactly one seed theme, so is_primary is unambiguously true here.
_THEMES: dict[str, tuple[str, int, list[str]]] = {
    "semis": ("Semiconductors", 1, ["NVDA", "AMD", "MU", "SNDK", "AVGO"]),
    "space": ("Space & Satellite", 2, ["ASTS", "RKLB", "LUNR"]),
}
_EFFECTIVE_FROM = date(2020, 1, 1)  # history far enough back for any 120d window

_UPSERT_THEME = text("""
    INSERT INTO themes (key, name, sort_order) VALUES (:key, :name, :sort_order)
    ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, sort_order = EXCLUDED.sort_order
    RETURNING id
""")

_HAS_MEMBER = text("""
    SELECT 1 FROM theme_members
    WHERE theme_id = :theme_id AND symbol = :symbol AND effective_to IS NULL
""")

_INSERT_MEMBER = text("""
    INSERT INTO theme_members (theme_id, symbol, weight, is_primary, effective_from)
    VALUES (:theme_id, :symbol, 1, true, :effective_from)
""")


def seed_themes(conn: Connection) -> int:
    """Insert (or refresh) the reference themes and their current members.
    Returns the number of newly-inserted member rows; re-running is a no-op."""
    written = 0
    for key, (name, sort_order, symbols) in _THEMES.items():
        theme_id = conn.execute(
            _UPSERT_THEME, {"key": key, "name": name, "sort_order": sort_order}
        ).scalar_one()
        for symbol in symbols:
            exists = conn.execute(
                _HAS_MEMBER, {"theme_id": theme_id, "symbol": symbol}
            ).first()
            if exists:
                continue
            conn.execute(_INSERT_MEMBER, {
                "theme_id": theme_id, "symbol": symbol, "effective_from": _EFFECTIVE_FROM,
            })
            written += 1
    return written
