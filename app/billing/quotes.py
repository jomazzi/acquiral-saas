"""Prices are data, not code. List prices, standing discounts, VAT and
campaigns live in the database (edited from the ops console) and every
checkout is priced from them. Paystack plans have fixed amounts, so each
distinct price a customer can be charged is an 'offer' with its own
Paystack plan, created on first use and reused afterwards. A subscriber
stays on the offer they signed up with, so later price, VAT or campaign
changes never reprice them."""
import logging

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.billing import paystack, plans
from app.billing.access import utcnow
from app.models.pricing import Bonus, Campaign, PriceListItem, PriceOffer, TaxSetting

from datetime import timedelta
from app.models.billing import SubscriptionPayment

log = logging.getLogger("acquiral.billing")
MAX_DISCOUNT_BP = 9000  # never more than 90% off, whatever is stored


# ---- pure arithmetic (no database) ----------------------------------------

def pct(amount, bp):
    """amount x bp/10000 in integer minor units, rounded half up."""
    return (amount * bp + 5000) // 10000


def campaign_applies(c, plan_key, interval, currency, now):
    return bool(c["active"] and c["starts_at"] <= now < c["ends_at"]
                and c.get("plan_key") in (None, plan_key)
                and c.get("interval") in (None, interval)
                and c.get("currency") in (None, currency))


def combined_bp(standing_bp, campaign):
    """Total discount in basis points. A stacking campaign adds to the
    standing discount; otherwise the bigger of the two applies."""
    if campaign is None:
        return min(standing_bp, MAX_DISCOUNT_BP)
    if campaign.get("stacks"):
        total = standing_bp + campaign["discount_bp"]
    else:
        total = max(standing_bp, campaign["discount_bp"])
    return min(total, MAX_DISCOUNT_BP)


def pick_campaign(campaigns, plan_key, interval, currency, standing_bp, now):
    """The running campaign that gives the customer the biggest total
    discount, if any beats the standing discount alone."""
    best, best_bp = None, combined_bp(standing_bp, None)
    for c in campaigns:
        if not campaign_applies(c, plan_key, interval, currency, now):
            continue
        bp = combined_bp(standing_bp, c)
        if bp > best_bp:
            best, best_bp = c, bp
    return best


def build_quote(plan_key, interval, currency, list_minor, standing_bp, campaign, vat_bp):
    """All amounts are integer kobo/cents. The discount is worked out in
    one step on the list price, then split into a standing line and a
    campaign line that always add up exactly."""
    total_bp = combined_bp(standing_bp, campaign)
    discount = pct(list_minor, total_bp)
    standing_minor = camp_minor = standing_shown = camp_shown = 0
    if campaign is None:
        standing_minor, standing_shown = discount, total_bp
    elif campaign.get("stacks"):
        standing_minor = min(pct(list_minor, standing_bp), discount)
        standing_shown = standing_bp if standing_minor else 0
        camp_minor = discount - standing_minor
        camp_shown = total_bp - standing_bp
    else:  # the campaign replaces the standing discount
        camp_minor, camp_shown = discount, total_bp
    net = list_minor - discount
    vat = pct(net, vat_bp)
    return {
        "plan_key": plan_key, "interval": interval, "currency": currency,
        "list_minor": list_minor,
        "standing_bp": standing_shown, "standing_minor": standing_minor,
        "campaign_bp": camp_shown if camp_minor else 0, "campaign_minor": camp_minor,
        "campaign_name": campaign["name"] if camp_minor else None,
        "campaign_ends_at": campaign["ends_at"] if camp_minor else None,
        "discount_bp": total_bp, "discount_minor": discount, "net_minor": net,
        "vat_bp": vat_bp, "vat_minor": vat, "total_minor": net + vat,
    }


# ---- database-backed ------------------------------------------------------

def _campaign_dicts():
    return [{"name": c.name, "discount_bp": int(c.discount_bp), "stacks": bool(c.stacks),
             "plan_key": c.plan_key, "interval": c.interval, "currency": c.currency,
             "starts_at": c.starts_at, "ends_at": c.ends_at, "active": bool(c.active)}
            for c in Campaign.query.filter(Campaign.active.is_(True)).all()]


def quote(plan_key, interval, currency, now=None):
    """What a customer would pay right now, or None if that plan, period
    and currency isn't for sale."""
    now = now or utcnow()
    item = PriceListItem.query.filter_by(plan_key=plan_key, interval=interval, currency=currency).first()
    if item is None or item.amount_minor <= 0:
        return None
    standing_bp = int(item.discount_bp or 0)
    camp = pick_campaign(_campaign_dicts(), plan_key, interval, currency, standing_bp, now)
    tax = db.session.get(TaxSetting, currency)
    vat_bp = int(tax.vat_bp) if tax and tax.enabled else 0
    return build_quote(plan_key, interval, currency, int(item.amount_minor), standing_bp, camp, vat_bp)


def banner(now=None):
    """The biggest running campaign, for the pricing page headline."""
    now = now or utcnow()
    best = None
    for c in _campaign_dicts():
        if c["starts_at"] <= now < c["ends_at"] and (best is None or c["discount_bp"] > best["discount_bp"]):
            best = c
    if best is None:
        return None
    return {"name": best["name"], "percent": f"{min(best['discount_bp'], MAX_DISCOUNT_BP) / 100:g}",
            "ends": best["ends_at"].strftime("%d %b %Y")}


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
def sellable_currencies():
    """Currencies customers can currently buy in (switched on in the ops
    console). Never empty, so the pages always have a currency to show."""
    on = {t.currency for t in TaxSetting.query.filter(TaxSetting.sellable.is_(True)).all()}
    found = [c for c in plans.CURRENCIES if c in on]
    return found or [plans.DEFAULT_CURRENCY]

# ---- bonus months ------------------------------------------------------------

MAX_BONUS_MONTHS = 12


def _bonus_dicts():
    return [{"name": b.name, "bonus_months": int(b.bonus_months), "plan_key": b.plan_key, "interval": b.interval,
             "currency": b.currency, "starts_at": b.starts_at, "ends_at": b.ends_at, "active": bool(b.active)}
            for b in Bonus.query.filter(Bonus.active.is_(True)).all()]


def running_bonuses(now=None):
    now = now or utcnow()
    return [b for b in _bonus_dicts() if b["starts_at"] <= now < b["ends_at"]]


def pick_bonus(bonuses, plan_key, interval, currency, now):
    """The running bonus that gives the most free months for this purchase."""
    best = None
    for b in bonuses:
        if campaign_applies(b, plan_key, interval, currency, now):
            if best is None or b["bonus_months"] > best["bonus_months"]:
                best = b
    return best


def bonus_eligible(org):
    """New subscribers only: an organisation that has never paid. (Comped
    and demo workspaces never qualify.)"""
    from app import set_tenant
    if org is None or org.is_demo or org.billing_status == "comped":
        return False
    set_tenant(org.id)  # payments are row-secured per organisation
    return SubscriptionPayment.query.filter_by(organization_id=org.id).first() is None


def bonus_for(org, plan_key, interval, currency, now=None):
    now = now or utcnow()
    if not bonus_eligible(org):
        return None
    return pick_bonus(running_bonuses(now), plan_key, interval, currency, now)


def bonus_banner(now=None):
    """The biggest running bonus, for the pricing and landing pages."""
    now = now or utcnow()
    best = None
    for b in running_bonuses(now):
        if best is None or b["bonus_months"] > best["bonus_months"]:
            best = b
    if best is None:
        return None
    n = best["bonus_months"]
    headline = (f"Pay for 12 months, get {12 + n}" if best["interval"] == "annual"
                else f"{n} bonus month{'s' if n != 1 else ''} free")
    return {"name": best["name"], "months": n, "interval": best["interval"], "headline": headline,
            "ends": (best["ends_at"] + timedelta(hours=1)).strftime("%d %b %Y")}  # shown in WAT