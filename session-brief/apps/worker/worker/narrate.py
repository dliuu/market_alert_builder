"""Stage ⑤ narrate: Claude writes the prose fields (``one_thing`` + per-name
``why``) and nothing else.

The stage is deliberately **non-fatal** (docs/02 pipeline, docs/04 narration
contract): if the Claude call fails for any reason — no API key, auth error,
timeout, malformed JSON — the brief is returned unchanged, tables-only, and is
still valid and sendable. That is M8's definition of done: revoking the API key
still produces a sendable brief.

The seam is a ``Narrator`` — a plain ``str -> str`` callable (prompt in, raw
JSON out) — so the logic (prompt building, strict response validation, merge)
is unit-testable without a network. ``default_narrator()`` binds the real Claude
call at the edge, or returns ``None`` when no key is configured.

Two rules the parser enforces so the model can only ever add prose (docs/04,
invariant 2 "the LLM never produces a number"):

- **No numbers.** Any narration string containing a digit is dropped — a digit
  is a hallucination surface; every figure is carried by the tables.
- **No stray keys.** A ``why`` for a symbol that isn't in the attribution rows,
  or any key the response schema doesn't define, is dropped.

Only ``one_thing`` and the attribution rows' ``why`` are populated here.
``sector_notes`` (docs/04) waits on a sector section, which the close brief
doesn't build yet.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from pydantic import BaseModel

from contracts.brief import BriefObject, Row
from contracts.brief import Id as SectionId

# prompt -> raw JSON text. The single impure edge lives in default_narrator().
Narrator = Callable[[str], str]

_HAS_DIGIT = re.compile(r"\d")

_SYSTEM = (
    "You are the writer for a personal daily market brief. You write short, "
    "causal, concrete prose that explains *why* the session went the way it "
    "did. You never write a number, percentage, price, ratio, or basis-point "
    "figure — the brief's tables carry every figure, and any digit you write is "
    "treated as an error and discarded. Respond with a single JSON object and "
    "nothing else."
)


class _Narration(BaseModel):
    """The narration contract (docs/04). Extra keys are ignored by Pydantic —
    "drop any key that doesn't match" comes for free."""

    one_thing: str | None = None
    why: dict[str, str] = {}


def build_prompt(obj: BriefObject) -> str:
    """The user turn: the computed object as context plus an explicit request for
    prose-only JSON. The model reads the figures but must not restate them."""
    rows = _attribution_rows(obj)
    symbols = sorted(row.symbol for row in rows if row.symbol is not None)
    lead = _lead_symbol(rows)
    lead_clause = (
        f" It must be about {lead}, today's single largest idiosyncratic mover."
        if lead is not None
        else ""
    )
    framing = _theme_framing(rows)
    context = json.dumps(obj.model_dump(mode="json"), indent=2, sort_keys=True)
    return (
        "Write the prose for today's close brief. Return ONLY a JSON object:\n"
        '  "one_thing": one short paragraph (2-3 sentences) naming the single '
        f"most consequential fact of the session and why it happened.{lead_clause}\n"
        f'  "why": an object mapping each of these tickers exactly — {symbols} — '
        "to one causal sentence explaining its move.\n\n"
        "Rules: prose only. Never write a number, percentage, price, or basis "
        "point — describe direction and cause in words and let the tables carry "
        "the figures. Do not invent news you were not given.\n\n"
        f"{framing}"
        "Session data, for context only — do not restate its figures:\n"
        f"{context}"
    )


def build_open_prompt(obj: BriefObject) -> str:
    """The open brief's user turn (M14). Same contract, opposite tense: the close
    brief explains a session that happened, the open brief reads the one about to
    start. It asks only for ``one_thing`` — docs/05 gives §4/§5/§6 no ``why``
    line, so requesting per-name prose would invite text the object cannot
    carry."""
    context = json.dumps(obj.model_dump(mode="json"), indent=2, sort_keys=True)
    return (
        "Write the prose for this morning's pre-open brief. Return ONLY a JSON "
        'object with one key:\n'
        '  "one_thing": one short paragraph (2-3 sentences) naming what matters '
        "most about the day ahead — what to be ready for, and why it matters for "
        "this book. Look forward, not back: this is written before the bell and "
        "there is no performance to report.\n\n"
        "Rules: prose only. Never write a number, percentage, price, or basis "
        "point — describe direction and cause in words and let the tables carry "
        "the figures. Do not invent news or events you were not given.\n\n"
        "The day's setup, for context only — do not restate its figures:\n"
        f"{context}"
    )


def narrate_open_and_apply(obj: BriefObject, narrator: Narrator | None) -> BriefObject:
    """Stage ⑤ for the open brief. Non-fatal on every path (D19), which is what
    keeps the always-sending open brief always sending."""
    if narrator is None:
        return obj
    try:
        narration = parse_narration(narrator(build_open_prompt(obj)), obj)
    except Exception:
        return obj  # non-fatal (docs/02): tables-only, still valid and sendable
    return apply_narration(obj, narration)


def parse_narration(raw: str, obj: BriefObject) -> _Narration:
    """Validate the model's reply against the strict schema, then drop anything
    that would smuggle a number in or name a symbol the object doesn't have.
    Raises on malformed JSON or a shape the schema rejects — the caller treats
    that as a non-event and ships tables-only."""
    data = json.loads(raw)
    narration = _Narration.model_validate(data)

    symbols = _attribution_symbols(obj)
    why = {
        symbol: prose
        for symbol, prose in narration.why.items()
        if symbol in symbols and not _HAS_DIGIT.search(prose)
    }
    one_thing = narration.one_thing
    if one_thing is not None and _HAS_DIGIT.search(one_thing):
        one_thing = None
    return _Narration(one_thing=one_thing, why=why)


def apply_narration(obj: BriefObject, narration: _Narration) -> BriefObject:
    """Merge the prose into a fresh, re-validated ``BriefObject``. Rows without
    a ``why`` keep their ``None`` — absence of prose is a valid brief."""
    payload = obj.model_dump(mode="json")
    if narration.one_thing is not None:
        payload["one_thing"] = narration.one_thing
    for section in payload["sections"]:
        if section["id"] == SectionId.attribution.value:
            for row in section["rows"]:
                prose = narration.why.get(row["symbol"])
                if prose is not None:
                    row["why"] = prose
    return BriefObject.model_validate(payload)


def narrate_and_apply(obj: BriefObject, narrator: Narrator | None) -> BriefObject:
    """Stage ⑤. Return ``obj`` with prose merged in, or ``obj`` unchanged when
    narration is unavailable or fails. The broad ``except`` is the feature, not a
    hedge: a brief with no prose is useful; a brief that didn't send is not."""
    if narrator is None:
        return obj
    try:
        narration = parse_narration(narrator(build_prompt(obj)), obj)
    except Exception:
        return obj  # non-fatal (docs/02): tables-only, still valid and sendable
    return apply_narration(obj, narration)


def _attribution_symbols(obj: BriefObject) -> set[str]:
    for section in obj.sections:
        if section.id is SectionId.attribution:
            # `symbol` is optional since v3 (a macro calendar row names no
            # security). Attribution rows always have one; skipping the None
            # case keeps the set typed and can only ever drop a row that has
            # no ticker for narration to key on.
            return {row.symbol for row in section.rows if row.symbol is not None}
    return set()


def _attribution_rows(obj: BriefObject) -> list[Row]:
    for section in obj.sections:
        if section.id is SectionId.attribution:
            return list(section.rows)
    return []


def _lead_symbol(rows: list[Row]) -> str | None:
    """The attribution row with the largest |resid_z| — the required subject
    of one_thing (docs/04: the residual-material name leads)."""
    lead: Row | None = None
    lead_abs = -1.0
    for row in rows:
        if row.resid_z is None:
            continue
        magnitude = abs(row.resid_z)
        if magnitude > lead_abs:
            lead_abs = magnitude
            lead = row
    return lead.symbol if lead is not None else None


def _theme_framing(rows: list[Row]) -> str:
    """A compact, number-free line per name: did it move with its theme, or on
    its own? Rows without both bps figures are skipped rather than guessed."""
    parts = []
    for row in sorted(rows, key=lambda r: r.symbol or ""):
        if row.theme_bps is None or row.resid_bps is None:
            continue
        moved = (
            "moved with the theme"
            if abs(row.theme_bps) >= abs(row.resid_bps)
            else "moved on its own"
        )
        parts.append(f"{row.symbol} {moved}")
    if not parts:
        return ""
    return "Context (words only, no figures): " + "; ".join(parts) + ".\n\n"


# --- The impure edge ------------------------------------------------------


def default_narrator() -> Narrator | None:
    """The production narrator, or ``None`` when no key is set — which is exactly
    the "revoke the API key" path in M8's definition of done."""
    from worker.config import ANTHROPIC_API_KEY

    if not ANTHROPIC_API_KEY:
        return None
    return _anthropic_generate


def _anthropic_generate(prompt: str) -> str:
    """The one Claude call. Prose only, so no thinking and a tight token cap;
    every figure is substituted from the object downstream (invariant 2)."""
    import anthropic

    from worker.config import ANTHROPIC_API_KEY, NARRATION_MODEL

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=NARRATION_MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if block.type == "text")
