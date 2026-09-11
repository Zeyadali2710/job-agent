"""applications

Revision ID: 0003_applications
Revises: 0002_profile_analysis
Create Date: 2026-09-10 12:45:44.994322+00:00
"""
from pathlib import Path

from alembic import op

revision = '0003_applications'
down_revision = '0002_profile_analysis'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0003_applications.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0003_applications.down.sql").read_text())
