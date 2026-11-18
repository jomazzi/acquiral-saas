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
from app.billing.access import utcnow
from app.billing import paystack, plans, quotes
from app.models.pricing import PriceOffer

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
    offer = quotes.offer_by_plan_code(plan.get("plan_code"))
    if offer is not None:
        return offer.plan_key, offer.interval, offer.currency
    return plans.lookup_by_plan_code(plan.get("plan_code"))  # older plans from settings


def offer_from_charge(data):
    """The offer a charge was made under: from checkout metadata on a
    first payment, from the plan code on a renewal."""
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    oid = _as_uuid(meta.get("offer_id"))
    if oid:
        offer = db.session.get(PriceOffer, oid)
        if offer is not None:
            return offer
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    return quotes.offer_by_plan_code(plan.get("plan_code"))


def breakdown_for(offer, meta, amount):
    """(offer_id, list, discount, vat) for a charge, or Nones when unknown.
    A plan-based charge uses its offer; a one-time payment carries its
    breakdown in the metadata we sent, and is trusted only if it adds up."""
    if offer is not None:
        return offer.id, offer.list_minor, offer.discount_minor, offer.vat_minor
    try:
        lst, disc, vat = int(meta["list_minor"]), int(meta["discount_minor"]), int(meta["vat_minor"])
    except (KeyError, TypeError, ValueError):
        return None, None, None, None
    if lst - disc + vat != amount:
        return None, None, None, None
    return None, lst, disc, vat


def _start_deferred_subscription(org, data, offer, start):
    """The first payment was a plain card charge, so bonus months could be
    added. Now schedule the recurring subscription to begin when the paid
    period, bonus included, ends. Never raises: the payment is already recorded."""
    auth = data.get("authorization") if isinstance(data.get("authorization"), dict) else {}
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    if offer is None or not auth.get("authorization_code") or not auth.get("reusable") \
            or not customer.get("customer_code"):
        log.warning("Org %s paid up front but automatic renewal can't be set up (reusable card: %s)",
                    org.id, bool(auth.get("reusable")))
        return False
    try:
        sub = paystack.create_subscription(
            customer=customer["customer_code"], plan_code=offer.paystack_plan_code,
            authorization=auth["authorization_code"], start_date=start)
        org.paystack_subscription_code = sub.get("subscription_code") or org.paystack_subscription_code
        org.paystack_email_token = sub.get("email_token") or org.paystack_email_token
        db.session.commit()
        return True
    except paystack.PaystackError as e:
        db.session.rollback()
        log.error("Could not schedule the subscription for org %s: %s", org.id, e)
    except Exception:
        db.session.rollback()
        log.exception("Could not record the subscription for org %s", org.id)
    return False


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
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    mode = meta.get("mode")
    is_once = mode == "once"
    amount = int(data.get("amount") or 0)
    offer = offer_from_charge(data)
    if offer is not None and offer.total_minor != amount:
        offer = None  # don't attach a breakdown that doesn't add up to what was charged
    offer_id, list_minor, discount_minor, vat_minor = breakdown_for(offer, meta, amount)

    # Tenant context for the RLS-secured payments table. The webhook has
    # no logged-in user, so nothing else has set it.
    set_tenant(org.id)
    if SubscriptionPayment.query.filter_by(reference=reference).first():
        db.session.rollback()
        return False

    # Bonus months: only on a new subscriber's first payment, and only if
    # we offered it (the amount comes from the metadata we sent at checkout).
    bonus_months, bonus_name = 0, None
    if mode in ("once", "subscribe_bonus") \
            and SubscriptionPayment.query.filter_by(organization_id=org.id).first() is None:
        try:
            bonus_months = max(0, min(int(meta.get("bonus_months") or 0), quotes.MAX_BONUS_MONTHS))
        except (TypeError, ValueError):
            bonus_months = 0
        bonus_name = (str(meta.get("bonus_name") or "")[:120] or None) if bonus_months else None

    db.session.add(SubscriptionPayment(
        organization_id=org.id, reference=reference,
        amount_minor=amount, currency=data.get("currency") or currency or "NGN",
        plan_key=plan_key, plan_interval=interval, paid_at=paid_at,
        offer_id=offer_id, list_minor=list_minor, discount_minor=discount_minor, vat_minor=vat_minor,
        bonus_months=bonus_months or None, bonus_name=bonus_name,
    ))

    # A one-time payment adds a period on top of whatever is already paid for.
    base = paid_at
    if (is_once and org.billing_status in ("active", "past_due", "canceled")
            and org.current_period_end and org.current_period_end > paid_at):
        base = org.current_period_end

    if plan_key:
        org.plan_key, org.plan_interval, org.plan_currency = plan_key, interval, currency
    if customer.get("customer_code"):
        org.paystack_customer_code = customer["customer_code"]
    if is_once:  # no recurring subscription behind this payment
        org.paystack_subscription_code = None
        org.paystack_email_token = None
    org.billing_status = "active"
    new_end = base + timedelta(days=plans.PERIOD_DAYS.get(org.plan_interval or "monthly", 31))
    if bonus_months:
        new_end = plans.add_months(new_end, bonus_months)
    # Never move the period backwards (a replayed older event).
    if not org.current_period_end or new_end > org.current_period_end:
        org.current_period_end = new_end
    period_end = org.current_period_end

    try:
        db.session.commit()
    except IntegrityError:
        # Lost a race with the other path (webhook vs callback) -- the
        # other one recorded it, which is all we wanted.
        db.session.rollback()
        return False
    if mode == "subscribe_bonus":
        _start_deferred_subscription(org, data, offer, period_end)
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
