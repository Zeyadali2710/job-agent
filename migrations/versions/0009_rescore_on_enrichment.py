"""rescore on enrichment

Revision ID: 0009_rescore_on_enrichment
Revises: 0008_current_job_scores
Create Date: 2026-09-12 16:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0009_rescore_on_enrichment'
down_revision = '0008_current_job_scores'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0009_rescore_on_enrichment.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0009_rescore_on_enrichment.down.sql").read_text())
