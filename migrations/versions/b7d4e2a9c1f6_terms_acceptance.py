"""record terms acceptance on users

Revision ID: b7d4e2a9c1f6
Revises: a1f3c9d2e7b4
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d4e2a9c1f6"
down_revision = "a1f3c9d2e7b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("terms_version", sa.String(20), nullable=True))


def downgrade():
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_accepted_at")