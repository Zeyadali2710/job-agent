"""approval

Revision ID: 0011_approval
Revises: 0010_generation
Create Date: 2026-09-13 06:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0011_approval'
down_revision = '0010_generation'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0011_approval.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0011_approval.down.sql").read_text())
