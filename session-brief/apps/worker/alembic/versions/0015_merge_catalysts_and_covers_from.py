"""Merge the two 0014 heads: M17's ``0014_catalysts`` and the ingest dedup fix
``0014_raw_payload_covers_from`` both branch off ``0013_claims_premarket_gap``
(developed in parallel), so the chain had two heads. Listing both as
``down_revision`` makes this the single head that reunifies them; neither
touches a table the other created, so the merge is purely topological — same
shape as ``0011_attribution_consumers``' merge of the two 0010 heads.

Revision ID: 0015_merge_catalysts_and_covers_from
Revises: 0014_catalysts, 0014_raw_payload_covers_from
Create Date: 2026-08-15

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0015_merge_catalysts_and_covers_from"
down_revision: str | Sequence[str] | None = (
    "0014_catalysts",
    "0014_raw_payload_covers_from",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
