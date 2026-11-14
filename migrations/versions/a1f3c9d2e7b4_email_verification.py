"""email verification columns on users

Revision ID: a1f3c9d2e7b4
Revises: 87773fec0a50
"""
from alembic import op
import sqlalchemy as sa

revision = "a1f3c9d2e7b4"
down_revision = "87773fec0a50"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(150), nullable=True))
    # server_default true: every existing user counts as verified.
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("users", sa.Column("verification_sent_at", sa.DateTime(), nullable=True))
    op.create_index("ix_users_email", "users", ["email"])


def downgrade():
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "verification_sent_at")
    op.drop_column("users", "email_verified")
    op.drop_column("users", "email")