"""api enqueue

Revision ID: 0012_api_enqueue
Revises: 0011_approval
Create Date: 2026-09-13 07:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0012_api_enqueue'
down_revision = '0011_approval'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0012_api_enqueue.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0012_api_enqueue.down.sql").read_text())
