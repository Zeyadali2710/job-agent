"""core

Revision ID: 0001_core
Revises: 
Create Date: 2026-09-10 12:45:42.071110+00:00
"""
from pathlib import Path

from alembic import op

revision = '0001_core'
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0001_core.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0001_core.down.sql").read_text())
