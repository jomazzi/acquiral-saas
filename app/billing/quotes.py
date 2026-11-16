"""Prices are data, not code. List prices, VAT and campaigns live in the
database (edited from the ops console) and every checkout is priced from
them. Paystack plans have fixed amounts, so each distinct price a customer
can be charged is an 'offer' with its own Paystack plan, created on first
use and reused afterwards. A subscriber stays on the offer they signed up
with, so later price, VAT or campaign changes never reprice them."""
import logging

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.billing import paystack, plans
from app.billing.access import utcnow
from app.models.pricing import Campaign, PriceListItem, PriceOffer, TaxSetting

log = logging.getLogger("acquiral.billing")
MAX_DISCOUNT_BP = 9000  # never more than 90% off, whatever is stored


def pct(amount, bp):
    """amount x bp/10000 in integer minor units, rounded half up."""
    return (amount * bp + 5000) // 10000


def active_campaigns(now=None):
    now = now or utcnow()
    return Campaign.query.filter(Campaign.active.is_(True), Campaign.starts_at <= now,
                                 Campaign.ends_at > now).all()


def best_campaign(plan_key, interval, currency, now=None):
    best = None
    for c in active_campaigns(now):
        if c.plan_key in (None, plan_key) and c.interval in (None, interval) and c.currency in (None, currency):
            if best is None or c.discount_bp > best.discount_bp:
                best = c
    return best


def quote(plan_key, interval, currency, now=None):
    """What a customer would pay right now, or None if that plan, period
    and currency isn't for sale. All amounts are integer kobo/cents."""
    item = PriceListItem.query.filter_by(plan_key=plan_key, interval=interval, currency=currency).first()
    if item is None or item.amount_minor <= 0:
        return None
    camp = best_campaign(plan_key, interval, currency, now)
    disc_bp = min(camp.discount_bp, MAX_DISCOUNT_BP) if camp else 0
    list_minor = int(item.amount_minor)
    discount = pct(list_minor, disc_bp)
    net = list_minor - discount
    tax = db.session.get(TaxSetting, currency)
    vat_bp = int(tax.vat_bp) if tax and tax.enabled else 0
    vat = pct(net, vat_bp)
    return {
        "plan_key": plan_key, "interval": interval, "currency": currency,
        "list_minor": list_minor, "discount_bp": disc_bp, "discount_minor": discount,
        "net_minor": net, "vat_bp": vat_bp, "vat_minor": vat, "total_minor": net + vat,
        "campaign_name": camp.name if camp else None,
        "campaign_ends_at": camp.ends_at if camp else None,
    }


def banner(now=None):
    """The best active campaign, for the pricing page headline."""
    best = None
    for c in active_campaigns(now):
        if best is None or c.discount_bp > best.discount_bp:
            best = c
    if best is None:
        return None
    return {"name": best.name, "percent": f"{min(best.discount_bp, MAX_DISCOUNT_BP) / 100:g}",
            "ends": best.ends_at.strftime("%d %b %Y")}


def get_or_create_offer(q):
    """The offer (and Paystack plan) for this exact quote. Raises
    paystack.PaystackError if the plan has to be created and Paystack refuses."""
    terms = dict(plan_key=q["plan_key"], interval=q["interval"], currency=q["currency"],
                 list_minor=q["list_minor"], discount_bp=q["discount_bp"], vat_bp=q["vat_bp"])
    offer = PriceOffer.query.filter_by(**terms).first()
    if offer is not None:
        return offer
    name = (f"Acquiral {plans.PLANS[q['plan_key']]['name']} ({q['interval']}, {q['currency']}) "
            f"{q['total_minor'] / 100:,.2f}")
    created = paystack.create_plan(name=name, amount_minor=q["total_minor"],
                                   interval=plans.PAYSTACK_INTERVAL[q["interval"]], currency=q["currency"])
    offer = PriceOffer(**terms, discount_minor=q["discount_minor"], net_minor=q["net_minor"],
                       vat_minor=q["vat_minor"], total_minor=q["total_minor"],
                       campaign_name=q["campaign_name"], paystack_plan_code=created["plan_code"])
    db.session.add(offer)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # another request created the same offer first
        offer = PriceOffer.query.filter_by(**terms).first()
        if offer is None:
            raise
    return offer


def offer_by_plan_code(plan_code):
    if not plan_code:
        return None
    return PriceOffer.query.filter_by(paystack_plan_code=plan_code).first()