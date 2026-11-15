"""ops console: administrator accounts and audit log

Revision ID: c5e8a1d3f7b2
Revises: b7d4e2a9c1f6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c5e8a1d3f7b2"
down_revision = "b7d4e2a9c1f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ops_admins",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.String(80), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("totp_secret_enc", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("last_totp_step", sa.BigInteger(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
    )
    op.create_table(
        "ops_audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("admin_username", sa.String(80), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
    )
    op.create_index("ix_ops_audit_log_created_at", "ops_audit_log", ["created_at"])


def downgrade():
    op.drop_index("ix_ops_audit_log_created_at", table_name="ops_audit_log")
    op.drop_table("ops_audit_log")
    op.drop_table("ops_admins")