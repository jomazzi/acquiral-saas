import uuid

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin


class SubscriptionPayment(TenantScopedMixin, db.Model):
    """One successful Paystack charge for an organisation's Acquiral
    subscription -- the billing history shown on the Billing page.

    Tenant-scoped (and RLS-secured, see scripts/setup_rls.sql). The
    webhook has no logged-in user, so it must call set_tenant(org.id)
    after resolving the organisation and before inserting here.
    `reference` is unique across ALL tenants: it is Paystack's own
    transaction reference, and the unique constraint is what makes
    webhook + callback processing idempotent (both fire for the same
    charge)."""
    __tablename__ = "subscription_payments"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference = db.Column(db.String(100), nullable=False, unique=True)
    amount_minor = db.Column(db.BigInteger, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="NGN")
    plan_key = db.Column(db.String(30))
    plan_interval = db.Column(db.String(10))
    paid_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
