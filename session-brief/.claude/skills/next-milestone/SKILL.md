---
name: next-milestone
description: Start work on the next unchecked milestone from docs/08-milestones.md. Use at the beginning of a work session, or when asked what to build next.
---

# Next milestone

## Steps

1. Read `docs/08-milestones.md` and find the first unchecked box.
2. Read the docs that milestone touches — not all of them. The mapping:
   - M1 → `03-data-model.md`
   - M2 → `02-architecture.md`, `03-data-model.md`
   - M3 → `03-data-model.md`
   - M4, M5, M6, M7 → `04-brief-object.md`, `05-content-spec.md`
   - M8 → `04-brief-object.md` (narration contract)
   - M9 → `06-email.md`, `design/design-reference.html`
   - M10 → `02-architecture.md` (scheduling)
3. Check `docs/07-decisions.md` for anything that constrains the approach.
4. Propose a plan and get agreement **before** writing code.
5. Build it. Definition of done is in the milestone line — treat it literally.
6. When done: tick the box, and append to `07-decisions.md` if a real decision was made.

## Constraints

The invariants in `CLAUDE.md` are not negotiable. If a milestone appears to require breaking one, that's a signal the design is wrong — raise it rather than working around it.
