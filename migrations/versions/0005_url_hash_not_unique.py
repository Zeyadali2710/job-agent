"""url_hash not unique

Revision ID: 0005_url_hash_not_unique
Revises: 0004_renderer_heartbeat_grant
Create Date: 2026-09-12 00:10:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0005_url_hash_not_unique'
down_revision = '0004_renderer_heartbeat_grant'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0005_url_hash_not_unique.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0005_url_hash_not_unique.down.sql").read_text())
