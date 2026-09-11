"""profile and analysis

Revision ID: 0002_profile_analysis
Revises: 0001_core
Create Date: 2026-09-10 12:45:43.545405+00:00
"""
from pathlib import Path

from alembic import op

revision = '0002_profile_analysis'
down_revision = '0001_core'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0002_profile_analysis.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0002_profile_analysis.down.sql").read_text())
