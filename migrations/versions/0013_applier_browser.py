"""applier browser

Revision ID: 0013_applier_browser
Revises: 0012_api_enqueue
Create Date: 2026-09-13 20:00:00.000000+00:00
"""
from pathlib import Path

from alembic import op

revision = '0013_applier_browser'
down_revision = '0012_api_enqueue'
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "0013_applier_browser.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "0013_applier_browser.down.sql").read_text())
