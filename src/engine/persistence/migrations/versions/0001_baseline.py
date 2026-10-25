"""baseline: empty schema

Stage 0 creates no tables. This revision gives Stage 2 a starting point so that it only ever
ADDS migrations: never edit a migration that has run (CLAUDE.md).

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
