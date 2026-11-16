"""standing discounts on the price list; campaigns can stack

Revision ID: e2b7c5a9d4f1
Revises: d9a4f2c8b1e3
"""
from alembic import op
import sqlalchemy as sa

revision = "e2b7c5a9d4f1"
down_revision = "d9a4f2c8b1e3"
branch_labels = None
depends_on = None

# (plan, currency, monthly, annual as seeded before). Annual becomes 12 x monthly
# with a 15% standing discount. Rows an admin has already edited are left alone.
ROWS = [("starter", "NGN", 5_500_000, 56_100_000), ("starter", "USD", 4_600, 46_900),
        ("organisation", "NGN", 7_900_000, 80_580_000), ("organisation", "USD", 5_700, 58_100)]


def upgrade():
    op.add_column("price_list", sa.Column("discount_bp", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_price_list_discount", "price_list", "discount_bp >= 0 AND discount_bp <= 9000")
    op.add_column("campaigns", sa.Column("stacks", sa.Boolean(), nullable=False, server_default=sa.false()))
    conn = op.get_bind()
    for plan, cur, monthly, old_annual in ROWS:
        conn.execute(sa.text(
            "update price_list set amount_minor = :new, discount_bp = 1500 "
            "where plan_key = :p and interval = 'annual' and currency = :c and amount_minor = :old"),
            {"new": monthly * 12, "p": plan, "c": cur, "old": old_annual})


def downgrade():
    conn = op.get_bind()
    for plan, cur, monthly, old_annual in ROWS:
        conn.execute(sa.text(
            "update price_list set amount_minor = :old where plan_key = :p and interval = 'annual' "
            "and currency = :c and amount_minor = :new and discount_bp = 1500"),
            {"new": monthly * 12, "p": plan, "c": cur, "old": old_annual})
    op.drop_column("campaigns", "stacks")
    op.drop_constraint("ck_price_list_discount", "price_list")
    op.drop_column("price_list", "discount_bp")