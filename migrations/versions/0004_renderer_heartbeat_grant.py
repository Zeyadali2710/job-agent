"""renderer heartbeat grant

Revision ID: 0004_renderer_heartbeat_grant
Revises: 0003_applications
Create Date: 2026-09-12 00:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0004_renderer_heartbeat_grant'
down_revision = '0003_applications'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0004_renderer_heartbeat_grant.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0004_renderer_heartbeat_grant.down.sql").read_text())
