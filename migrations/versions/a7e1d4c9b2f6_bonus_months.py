"""bonus months for new subscribers

Revision ID: a7e1d4c9b2f6
Revises: f4c8d1a7e3b9
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a7e1d4c9b2f6"
down_revision = "f4c8d1a7e3b9"
branch_labels = None
depends_on = None

NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    op.create_table(
        "bonuses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("bonus_months", sa.Integer(), nullable=False),
        sa.Column("plan_key", sa.String(30)),
        sa.Column("interval", sa.String(10)),
        sa.Column("currency", sa.String(3)),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(80)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW),
        sa.CheckConstraint("bonus_months >= 1 AND bonus_months <= 12", name="ck_bonus_months"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_bonus_dates"),
    )
    op.add_column("subscription_payments", sa.Column("bonus_months", sa.Integer(), nullable=True))
    op.add_column("subscription_payments", sa.Column("bonus_name", sa.String(120), nullable=True))


def downgrade():
    op.drop_column("subscription_payments", "bonus_name")
    op.drop_column("subscription_payments", "bonus_months")
    op.drop_table("bonuses")