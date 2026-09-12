"""backfill group location_class

Revision ID: 0006_backfill_group_location
Revises: 0005_url_hash_not_unique
Create Date: 2026-09-12 12:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0006_backfill_group_location'
down_revision = '0005_url_hash_not_unique'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0006_backfill_group_location.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0006_backfill_group_location.down.sql").read_text())
