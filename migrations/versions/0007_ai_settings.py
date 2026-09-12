"""ai settings

Revision ID: 0007_ai_settings
Revises: 0006_backfill_group_location
Create Date: 2026-09-12 14:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0007_ai_settings'
down_revision = '0006_backfill_group_location'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0007_ai_settings.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0007_ai_settings.down.sql").read_text())
