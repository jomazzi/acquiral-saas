import uuid

from sqlalchemy.dialects.postgresql import UUID

from app.extensions import db

NOW_UTC = db.func.now()


class PriceListItem(db.Model):
    """The list price (before discounts and VAT) of one plan, interval and currency."""
    __tablename__ = "price_list"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_key = db.Column(db.String(30), nullable=False)
    interval = db.Column(db.String(10), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    amount_minor = db.Column(db.BigInteger, nullable=False)
    discount_bp = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, server_default=NOW_UTC)
    updated_by = db.Column(db.String(80))
    __table_args__ = (db.UniqueConstraint("plan_key", "interval", "currency", name="uq_price_list_key"),)


class TaxSetting(db.Model):
    """VAT per currency. vat_bp is in basis points: 750 = 7.5%."""
    __tablename__ = "tax_settings"
    currency = db.Column(db.String(3), primary_key=True)
    vat_bp = db.Column(db.Integer, nullable=False, default=0)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, server_default=NOW_UTC)
    updated_by = db.Column(db.String(80))
    sellable = db.Column(db.Boolean, nullable=False, default=True)

class Campaign(db.Model):
    """A percentage discount for a date range. discount_bp: 2000 = 20%.
    plan_key / interval / currency left empty means 'all'."""
    __tablename__ = "campaigns"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(120), nullable=False)
    discount_bp = db.Column(db.Integer, nullable=False)
    stacks = db.Column(db.Boolean, nullable=False, default=False)
    plan_key = db.Column(db.String(30))
    interval = db.Column(db.String(10))
    currency = db.Column(db.String(3))
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, server_default=NOW_UTC)


class PriceOffer(db.Model):
    """One distinct price a customer can be charged, with its own Paystack
    plan. Created on first use, never edited: subscribers stay on theirs."""
    __tablename__ = "price_offers"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_key = db.Column(db.String(30), nullable=False)
    interval = db.Column(db.String(10), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    list_minor = db.Column(db.BigInteger, nullable=False)
    discount_bp = db.Column(db.Integer, nullable=False, default=0)
    discount_minor = db.Column(db.BigInteger, nullable=False, default=0)
    net_minor = db.Column(db.BigInteger, nullable=False)
    vat_bp = db.Column(db.Integer, nullable=False, default=0)
    vat_minor = db.Column(db.BigInteger, nullable=False, default=0)
    total_minor = db.Column(db.BigInteger, nullable=False)
    campaign_name = db.Column(db.String(120))
    paystack_plan_code = db.Column(db.String(60), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, server_default=NOW_UTC)
    __table_args__ = (db.UniqueConstraint("plan_key", "interval", "currency", "list_minor", "discount_bp",
                                          "vat_bp", name="uq_price_offer_terms"),)

class Bonus(db.Model):
    """Free extra months for a new subscriber's first payment, for a date
    range. plan_key / interval / currency left empty means 'all'."""
    __tablename__ = "bonuses"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(120), nullable=False)
    bonus_months = db.Column(db.Integer, nullable=False)
    plan_key = db.Column(db.String(30))
    interval = db.Column(db.String(10))
    currency = db.Column(db.String(3))
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, server_default=NOW_UTC)

