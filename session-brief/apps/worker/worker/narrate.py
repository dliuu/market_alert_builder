"""Stage ⑤ narrate: Claude writes the prose fields (``one_thing`` + per-name
``why`` + the open brief's ``tape_read``) and nothing else.

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
- **No stray keys.** A ``why`` for a symbol that isn't in the attribution rows
  (close) or the pre-market rows (open), or any key the response schema
  doesn't define, is dropped.

``one_thing``, the attribution/pre-market rows' ``why``, and the open brief's
§2 ``tape_read`` (M15) are populated here. ``sector_notes`` (docs/04) waits on
a sector section, which the close brief doesn't build yet.
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
    # §2's "read" paragraph (M15). It lands in the overnight_tape section's
    # `note`, which is why it needs no schema change — the section already
    # carries a note, and the digit guard applies to it like everything else.
    tape_read: str | None = None


def build_prompt(obj: BriefObject) -> str:
    """The user turn: the computed object as context plus an explicit request for
    prose-only JSON. The model reads the figures but must not restate them."""
    # The close prompt keys off attribution rows specifically — M13's lead and
    # theme framing are about the decomposition. `_narratable_symbols` (used by
    # the parser and the open prompt) is the generalization of the same set.
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
    start. It asks for ``one_thing``, §2's ``tape_read``, and §3's per-name
    ``why`` — docs/05 gives §4/§5/§6 no ``why`` line, so requesting prose for
    those would invite text the object cannot carry.

    ``tape_read`` is only asked for when §2 actually has rows (C1, M15 review).
    An empty §2 renders the sentinel "no tape captured" note; asking the model to
    read a tape it wasn't given invites it to invent one, and the digit guard
    can't catch prose. Not asking is the fix at the source — ``apply_narration``
    also refuses to merge a stray ``tape_read`` onto an empty section as a
    second, independent guard.
    """
    symbols = sorted(_narratable_symbols(obj))
    context = json.dumps(obj.model_dump(mode="json"), indent=2, sort_keys=True)
    tape_ask = (
        '  "tape_read": one short paragraph reading the overnight tape against '
        "this book's sectors — what the macro backdrop implies for how these "
        "names open. Direction and cause in words; no figures.\n"
        if _has_tape_rows(obj)
        else ""
    )
    return (
        "Write the prose for this morning's pre-open brief. Return ONLY a JSON "
        "object:\n"
        '  "one_thing": one short paragraph (2-3 sentences) naming what matters '
        "most about the day ahead — what to be ready for, and why it matters for "
        "this book. Look forward, not back: this is written before the bell and "
        "there is no performance to report.\n"
        f"{tape_ask}"
        f'  "why": an object mapping each of these tickers exactly — {symbols} — '
        "to one causal sentence explaining its pre-market move.\n"
        "Lead `one_thing` with the first pre-market row: it is the largest gap "
        "in the book this morning.\n\n"
        "Rules: prose only. Never write a number, percentage, price, or basis "
        "point — describe direction and cause in words and let the tables carry "
        "the figures. Do not invent news or events you were not given.\n\n"
        "The day's setup, for context only — do not restate its figures:\n"
        f"{context}"
    )


def _has_tape_rows(obj: BriefObject) -> bool:
    """Whether §2 (overnight_tape) carries any rows this session."""
    return any(
        section.id == SectionId.overnight_tape and len(section.rows) > 0
        for section in obj.sections
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

    symbols = _narratable_symbols(obj)
    why = {
        symbol: prose
        for symbol, prose in narration.why.items()
        if symbol in symbols and not _HAS_DIGIT.search(prose)
    }
    one_thing = narration.one_thing
    if one_thing is not None and _HAS_DIGIT.search(one_thing):
        one_thing = None
    tape_read = narration.tape_read
    if tape_read is not None and _HAS_DIGIT.search(tape_read):
        tape_read = None
    return _Narration(one_thing=one_thing, why=why, tape_read=tape_read)


def apply_narration(obj: BriefObject, narration: _Narration) -> BriefObject:
    """Merge the prose into a fresh, re-validated ``BriefObject``. Rows without
    a ``why`` keep their ``None`` — absence of prose is a valid brief."""
    payload = obj.model_dump(mode="json")
    if narration.one_thing is not None:
        payload["one_thing"] = narration.one_thing
    for section in payload["sections"]:
        if section["id"] in {s.value for s in _WHY_SECTIONS}:
            for row in section["rows"]:
                prose = narration.why.get(row["symbol"])
                if prose is not None:
                    row["why"] = prose
        # Gated on rows, not just a truthy tape_read (C1, M15 review): an empty
        # §2 already carries the honest "no tape captured" note, and asking the
        # model for a read of nothing invites hallucinated prose the digit guard
        # can't catch — it's not a number, it's a fabricated paragraph. This is
        # the second, independent half of the fix; build_open_prompt stops
        # asking for tape_read at the source when there are no rows.
        if (
            section["id"] == SectionId.overnight_tape.value
            and narration.tape_read
            and section["rows"]
        ):
            section["note"] = narration.tape_read
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


_WHY_SECTIONS = (SectionId.attribution, SectionId.premarket)


def _narratable_symbols(obj: BriefObject) -> set[str]:
    """Symbols narration may key a `why` to: the close brief's attribution rows
    and the open brief's pre-market rows. A brief has one or the other."""
    out: set[str] = set()
    for section in obj.sections:
        if section.id in _WHY_SECTIONS:
            # `symbol` is optional since v3 (a macro calendar row names no
            # security). Attribution and pre-market rows always have one;
            # skipping the None case keeps the set typed and can only ever
            # drop a row that has no ticker for narration to key on.
            out |= {row.symbol for row in section.rows if row.symbol is not None}
    return out


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
