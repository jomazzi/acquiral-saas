"""Applies Paystack payment/subscription events to an Organization.

Two entry points feed this -- the signed webhook (the source of truth,
and the only thing that sees renewals) and the post-checkout callback
(instant feedback for the customer who just paid). Both can fire for the
same charge, so everything here is idempotent: SubscriptionPayment.reference
is unique, and state updates only ever move current_period_end forward.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.tenant import Organization
from app.models.billing import SubscriptionPayment
from app.billing import plans
from app.billing.access import utcnow

log = logging.getLogger("acquiral.billing")


def parse_paystack_time(value):
    """Paystack timestamps are ISO-8601 UTC, e.g. 2026-10-08T12:00:00.000Z
    (sometimes with +00:00). Returns naive UTC, matching the DB columns."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _as_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def resolve_org(data):
    """Find which tenant a Paystack event belongs to, or None.

    Returns None (and the event is ignored) for anything we can't tie to
    an Acquiral organisation -- the same Paystack account may be used for
    other products, and their charges must never touch a tenant."""
    meta = data.get("metadata") or {}
    if isinstance(meta, dict):
        org_id = _as_uuid(meta.get("organization_id"))
        if org_id:
            org = db.session.get(Organization, org_id)
            if org:
                return org

    sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    sub_code = data.get("subscription_code") or sub.get("subscription_code")
    if sub_code:
        org = Organization.query.filter_by(paystack_subscription_code=sub_code).first()
        if org:
            return org

    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    cust_code = customer.get("customer_code")
    if cust_code:
        org = Organization.query.filter_by(paystack_customer_code=cust_code).first()
        if org:
            return org

    # Last resort for subscription.create, which can arrive before the
    # charge that links the customer code: match on the billing email we
    # sent, but only if it identifies exactly one real tenant.
    email = (customer.get("email") or "").strip().lower()
    if email:
        matches = Organization.query.filter(
            db.func.lower(Organization.billing_email) == email,
            Organization.is_demo.is_(False),
        ).limit(2).all()
        if len(matches) == 1:
            return matches[0]
    return None


def plan_from_charge(data):
    """(plan_key, interval, currency) for a charge -- from checkout
    metadata on a first payment, or from the Paystack plan code on a
    renewal. Any element may be None if it can't be determined."""
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    key, interval, currency = meta.get("plan_key"), meta.get("interval"), meta.get("currency")
    if key in plans.PLANS and interval in plans.INTERVALS and currency in plans.CURRENCIES:
        return key, interval, currency
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    return plans.lookup_by_plan_code(plan.get("plan_code"))


def apply_successful_charge(org, data):
    """Record a successful charge and (re)activate the subscription.
    Returns True if this charge was new, False if already processed."""
    from app import set_tenant

    reference = data.get("reference")
    if not reference or data.get("status") != "success":
        return False

    paid_at = parse_paystack_time(data.get("paid_at") or data.get("paidAt")) or utcnow()
    plan_key, interval, currency = plan_from_charge(data)
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}

    # Tenant context for the RLS-secured payments table. The webhook has
    # no logged-in user, so nothing else has set it.
    set_tenant(org.id)
    if SubscriptionPayment.query.filter_by(reference=reference).first():
        db.session.rollback()
        return False

    db.session.add(SubscriptionPayment(
        organization_id=org.id, reference=reference,
        amount_minor=int(data.get("amount") or 0), currency=data.get("currency") or currency or "NGN",
        plan_key=plan_key, plan_interval=interval, paid_at=paid_at,
    ))

    if plan_key:
        org.plan_key, org.plan_interval, org.plan_currency = plan_key, interval, currency
    if customer.get("customer_code"):
        org.paystack_customer_code = customer["customer_code"]
    org.billing_status = "active"
    new_end = paid_at + timedelta(days=plans.PERIOD_DAYS.get(org.plan_interval or "monthly", 31))
    # Never move the period backwards (a replayed older event).
    if not org.current_period_end or new_end > org.current_period_end:
        org.current_period_end = new_end

    try:
        db.session.commit()
    except IntegrityError:
        # Lost a race with the other path (webhook vs callback) -- the
        # other one recorded it, which is all we wanted.
        db.session.rollback()
        return False
    return True


def handle_event(event, data):
    """Dispatch one verified webhook event. Always safe to call twice."""
    org = resolve_org(data)
    if org is None:
        return "ignored: no matching organisation"
    if org.is_demo:
        return "ignored: demo organisation"

    if event == "charge.success":
        new = apply_successful_charge(org, data)
        return "payment recorded" if new else "duplicate charge ignored"

    if event == "subscription.create":
        customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
        org.paystack_subscription_code = data.get("subscription_code") or org.paystack_subscription_code
        org.paystack_email_token = data.get("email_token") or org.paystack_email_token
        org.paystack_customer_code = customer.get("customer_code") or org.paystack_customer_code
        nxt = parse_paystack_time(data.get("next_payment_date"))
        if nxt:
            org.current_period_end = nxt
        if org.billing_status in ("trialing", "canceled", "past_due"):
            org.billing_status = "active"
        db.session.commit()
        return "subscription linked"

    if event in ("subscription.disable", "subscription.not_renew"):
        org.billing_status = "canceled"
        db.session.commit()
        return "subscription cancelled (access continues to period end)"

    if event == "invoice.payment_failed":
        if org.billing_status == "active":
            org.billing_status = "past_due"
            db.session.commit()
        return "marked past due"

    return f"ignored: unhandled event {event}"
