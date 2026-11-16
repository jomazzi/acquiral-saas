"""dynamic pricing: price list, VAT settings, campaigns, price offers

Revision ID: d9a4f2c8b1e3
Revises: c5e8a1d3f7b2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d9a4f2c8b1e3"
down_revision = "c5e8a1d3f7b2"
branch_labels = None
depends_on = None

NOW = sa.text("(now() at time zone 'utc')")
NEW_UUID = sa.text("gen_random_uuid()")


def upgrade():
    price_list = op.create_table(
        "price_list",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=NEW_UUID),
        sa.Column("plan_key", sa.String(30), nullable=False),
        sa.Column("interval", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=NOW),
        sa.Column("updated_by", sa.String(80)),
        sa.UniqueConstraint("plan_key", "interval", "currency", name="uq_price_list_key"),
        sa.CheckConstraint("amount_minor > 0", name="ck_price_list_positive"),
    )
    # Seed with the prices that were hard-coded until now, in kobo / cents.
    seed = [
        ("starter", "monthly", "NGN", 5_500_000), ("starter", "annual", "NGN", 56_100_000),
        ("starter", "monthly", "USD", 4_600), ("starter", "annual", "USD", 46_900),
        ("organisation", "monthly", "NGN", 7_900_000), ("organisation", "annual", "NGN", 80_580_000),
        ("organisation", "monthly", "USD", 5_700), ("organisation", "annual", "USD", 58_100),
    ]
    op.bulk_insert(price_list, [{"plan_key": p, "interval": i, "currency": c, "amount_minor": a}
                                for p, i, c, a in seed])

    tax = op.create_table(
        "tax_settings",
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("vat_bp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=NOW),
        sa.Column("updated_by", sa.String(80)),
        sa.CheckConstraint("vat_bp >= 0 AND vat_bp <= 5000", name="ck_tax_bp"),
    )
    # VAT starts SWITCHED OFF everywhere: enable it only after your accountant confirms.
    op.bulk_insert(tax, [{"currency": "NGN", "vat_bp": 750, "enabled": False},
                         {"currency": "USD", "vat_bp": 0, "enabled": False}])

    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=NEW_UUID),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("discount_bp", sa.Integer(), nullable=False),
        sa.Column("plan_key", sa.String(30)),
        sa.Column("interval", sa.String(10)),
        sa.Column("currency", sa.String(3)),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(80)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW),
        sa.CheckConstraint("discount_bp >= 1 AND discount_bp <= 9000", name="ck_campaign_bp"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_campaign_dates"),
    )

    op.create_table(
        "price_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=NEW_UUID),
        sa.Column("plan_key", sa.String(30), nullable=False),
        sa.Column("interval", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("list_minor", sa.BigInteger(), nullable=False),
        sa.Column("discount_bp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discount_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("net_minor", sa.BigInteger(), nullable=False),
        sa.Column("vat_bp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vat_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("campaign_name", sa.String(120)),
        sa.Column("paystack_plan_code", sa.String(60), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=NOW),
        sa.UniqueConstraint("plan_key", "interval", "currency", "list_minor", "discount_bp", "vat_bp",
                            name="uq_price_offer_terms"),
    )

    op.add_column("subscription_payments", sa.Column("offer_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("subscription_payments", sa.Column("list_minor", sa.BigInteger(), nullable=True))
    op.add_column("subscription_payments", sa.Column("discount_minor", sa.BigInteger(), nullable=True))
    op.add_column("subscription_payments", sa.Column("vat_minor", sa.BigInteger(), nullable=True))


def downgrade():
    for col in ("vat_minor", "discount_minor", "list_minor", "offer_id"):
        op.drop_column("subscription_payments", col)
    op.drop_table("price_offers")
    op.drop_table("campaigns")
    op.drop_table("tax_settings")
    op.drop_table("price_list")