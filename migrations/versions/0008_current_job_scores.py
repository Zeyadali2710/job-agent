"""current job scores view

Revision ID: 0008_current_job_scores
Revises: 0007_ai_settings
Create Date: 2026-09-12 15:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0008_current_job_scores'
down_revision = '0007_ai_settings'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0008_current_job_scores.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0008_current_job_scores.down.sql").read_text())
