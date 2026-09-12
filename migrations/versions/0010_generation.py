"""generation

Revision ID: 0010_generation
Revises: 0009_rescore_on_enrichment
Create Date: 2026-09-13 04:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0010_generation'
down_revision = '0009_rescore_on_enrichment'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0010_generation.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0010_generation.down.sql").read_text())
