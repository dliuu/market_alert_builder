"""Merge the two post-0013 heads: ``0014_raw_payload_covers_from`` and the CN
branch (``0014_catalysts`` → ``0015_cn_book`` → ``0016_cn_brief_kinds``).

The covers-from fix and CN-M1 were developed in parallel sessions off the same
base; each branch is internally linear and their DDL is disjoint
(`raw_payloads.covers_from` vs `sectors.market` + the `briefs` kind CHECK), so
this is a pure merge revision — no DDL of its own, exactly the
``0011_attribution_consumers`` precedent for the two 0010 heads.
"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision: str = "0017_merge_covers_from_and_cn"
down_revision: tuple[str, str] | None = (
    "0016_cn_brief_kinds",
    "0014_raw_payload_covers_from",
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
