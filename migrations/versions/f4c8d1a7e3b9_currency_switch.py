"""per-currency 'sell in this currency' switch

Revision ID: f4c8d1a7e3b9
Revises: e2b7c5a9d4f1
"""
from alembic import op
import sqlalchemy as sa

revision = "f4c8d1a7e3b9"
down_revision = "e2b7c5a9d4f1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tax_settings", sa.Column("sellable", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    op.drop_column("tax_settings", "sellable")