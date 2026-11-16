"""Dynamic pricing: quotes, campaigns, VAT, on-demand Paystack plans,
grandfathering, and checkout.   PYTHONPATH=. python scripts/pricing_test.py
Run only against a scratch database."""
import os
import uuid
from datetime import timedelta

os.environ.pop("PAYSTACK_SECRET_KEY", None)
os.environ.pop("MAIL_SERVER", None)

from app import create_app, set_tenant
from app.extensions import db
from app.billing import paystack, quotes, service
from app.billing.access import utcnow
from app.models.billing import SubscriptionPayment
from app.models.pricing import Campaign, PriceListItem, PriceOffer, TaxSetting
from app.models.tenant import Organization

app = create_app()
app.app_context().push()
RUN = uuid.uuid4().hex[:6]
NOW = utcnow()
created_plans, inits, made_campaigns = [], [], []


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def fake_create_plan(name, amount_minor, interval, currency):
    code = f"PLN_pt_{uuid.uuid4().hex[:10]}"
    created_plans.append({"name": name, "amount_minor": amount_minor, "code": code})
    return {"plan_code": code}


def fake_init(**kw):
    inits.append(kw)
    return {"authorization_url": "https://checkout.paystack.test/x", "reference": kw["reference"]}


paystack.create_plan = fake_create_plan
paystack.initialize_transaction = fake_init


def price_row():
    return PriceListItem.query.filter_by(plan_key="starter", interval="monthly", currency="NGN").one()


def vat(enabled, bp=750):
    t = db.session.get(TaxSetting, "NGN")
    t.enabled, t.vat_bp = enabled, bp
    db.session.commit()


def camp(name, bp, **kw):
    c = Campaign(name=name, discount_bp=bp, starts_at=kw.get("starts", NOW - timedelta(days=1)),
                 ends_at=kw.get("ends", NOW + timedelta(days=1)), active=kw.get("active", True),
                 plan_key=kw.get("plan_key"), created_by="test")
    db.session.add(c)
    db.session.commit()
    made_campaigns.append(c.id)
    return c


ORIG_PRICE = price_row().amount_minor
_t = db.session.get(TaxSetting, "NGN")
ORIG_TAX = (_t.vat_bp, _t.enabled)


def run():
    print("--- Rounding ---")
    check(quotes.pct(1000, 750) == 75, "7.5% of 1000 is 75")
    check(quotes.pct(1001, 750) == 75, "7.5% of 1001 rounds to 75")
    check(quotes.pct(1, 5000) == 1, "a half rounds up")

    print("\n--- Quotes ---")
    q = quotes.quote("starter", "monthly", "NGN", NOW)
    check(q["list_minor"] == 5_500_000 and q["discount_minor"] == 0 and q["vat_minor"] == 0
          and q["total_minor"] == 5_500_000, "default quote is the list price, no VAT")
    check(quotes.quote("starter", "weekly", "NGN") is None, "an unknown period isn't for sale")
    vat(True)
    q = quotes.quote("starter", "monthly", "NGN", NOW)
    check(q["vat_minor"] == 412_500 and q["total_minor"] == 5_912_500, "VAT 7.5% added on top")
    vat(False)
    check(quotes.quote("starter", "monthly", "NGN", NOW)["vat_minor"] == 0, "VAT switched off adds nothing")

    print("\n--- Campaigns ---")
    camp(f"BF {RUN}", 2000)
    q = quotes.quote("starter", "monthly", "NGN", NOW)
    check(q["discount_minor"] == 1_100_000 and q["net_minor"] == 4_400_000 and q["campaign_name"] == f"BF {RUN}",
          "a 20% campaign applies")
    check(quotes.quote("starter", "monthly", "NGN", NOW + timedelta(days=3))["discount_minor"] == 0,
          "it stops when it ends")
    camp(f"Org only {RUN}", 3000, plan_key="organisation")
    check(quotes.quote("starter", "monthly", "NGN", NOW)["discount_bp"] == 2000, "a campaign for another plan doesn't apply")
    check(quotes.quote("organisation", "monthly", "NGN", NOW)["discount_bp"] == 3000, "the biggest applicable discount wins")
    camp(f"Off {RUN}", 5000, active=False)
    camp(f"Future {RUN}", 5000, starts=NOW + timedelta(days=2), ends=NOW + timedelta(days=4))
    check(quotes.quote("starter", "monthly", "NGN", NOW)["discount_bp"] == 2000, "inactive and future campaigns are ignored")
    vat(True)
    q = quotes.quote("starter", "monthly", "NGN", NOW)
    check(q["vat_minor"] == 330_000 and q["total_minor"] == 4_730_000, "VAT is charged on the discounted price")

    print("\n--- Offers and Paystack plans ---")
    before = len(created_plans)
    offer1 = quotes.get_or_create_offer(q)
    check(len(created_plans) == before + 1 and created_plans[-1]["amount_minor"] == 4_730_000
          and offer1.total_minor == 4_730_000, "a new price creates a Paystack plan at the exact total")
    again = quotes.get_or_create_offer(quotes.quote("starter", "monthly", "NGN", NOW))
    check(again.id == offer1.id and len(created_plans) == before + 1, "the same price reuses the plan")
    row = price_row()
    row.amount_minor = 6_000_000
    db.session.commit()
    q2 = quotes.quote("starter", "monthly", "NGN", NOW)
    check(q2["list_minor"] == 6_000_000, "editing the list price changes new quotes")
    offer2 = quotes.get_or_create_offer(q2)
    check(offer2.id != offer1.id and offer2.paystack_plan_code != offer1.paystack_plan_code, "a new price gets its own plan")
    check(db.session.get(PriceOffer, offer1.id).total_minor == 4_730_000, "the old offer is untouched (subscribers keep their price)")
    row.amount_minor = ORIG_PRICE
    db.session.commit()
    check(service.plan_from_charge({"plan": {"plan_code": offer1.paystack_plan_code}}) == ("starter", "monthly", "NGN"),
          "a renewal is recognised from the offer's plan code")

    print("\n--- A payment keeps its breakdown ---")
    ORG = f"Pricing Org {RUN}"
    c = app.test_client()
    r = c.post("/signup", data={"org_name": ORG, "username": f"p{RUN}", "full_name": "T", "email": f"pt-{RUN}@example.test",
                                "password": "password123", "accept_terms": "on"})
    check(r.status_code == 302, "test organisation signed up")
    org = Organization.query.filter_by(name=ORG).one()
    ref = f"pt_{RUN}"
    service.apply_successful_charge(org, {
        "status": "success", "reference": ref, "amount": offer1.total_minor, "currency": "NGN",
        "paid_at": utcnow().isoformat() + "Z", "customer": {"customer_code": f"CUS_PT_{RUN}"},
        "metadata": {"organization_id": str(org.id), "plan_key": "starter", "interval": "monthly",
                     "currency": "NGN", "offer_id": str(offer1.id)}})
    set_tenant(org.id)
    p = SubscriptionPayment.query.filter_by(reference=ref).one()
    check(p.amount_minor == 4_730_000 and p.list_minor == 5_500_000 and p.discount_minor == 1_100_000
          and p.vat_minor == 330_000, "the payment stores list, discount and VAT")

    print("\n--- Pages ---")
    r = app.test_client().get("/pricing")
    check(f"BF {RUN}".encode() in r.data and b"<s " in r.data and b"exclude VAT" in r.data,
          "pricing page shows the campaign, the crossed-out list price and the VAT note")

    print("\n--- Checkout ---")
    os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_pricing_unit"
    q = quotes.quote("starter", "monthly", "NGN")
    r = c.get("/billing")
    check(r.status_code == 200 and b"quote-box" in r.data and b"47,300" in r.data, "billing page shows the total with VAT")
    r = c.post("/billing/checkout", data={"plan": "starter", "interval": "monthly", "currency": "NGN",
                                          "email": "fin@pt.org", "expected_total": "1"})
    check(r.status_code == 302 and "/billing" in r.headers["Location"] and not inits, "a stale total is refused before Paystack is called")
    r = c.post("/billing/checkout", data={"plan": "starter", "interval": "monthly", "currency": "NGN",
                                          "email": "fin@pt.org", "expected_total": str(q["total_minor"])})
    check(r.status_code == 302 and r.headers["Location"].startswith("https://checkout.paystack.test/") and len(inits) == 1,
          "a matching total proceeds to Paystack")
    check(inits[-1]["amount_minor"] == q["total_minor"] and inits[-1]["plan_code"].startswith("PLN_pt_")
          and inits[-1]["metadata"].get("offer_id"), "it charges the quoted total on the offer's plan")
    os.environ.pop("PAYSTACK_SECRET_KEY", None)


def cleanup():
    db.session.rollback()
    for cid in made_campaigns:
        Campaign.query.filter_by(id=cid).delete()
    t = db.session.get(TaxSetting, "NGN")
    t.vat_bp, t.enabled = ORIG_TAX
    price_row().amount_minor = ORIG_PRICE
    PriceOffer.query.filter(PriceOffer.paystack_plan_code.like("PLN_pt_%")).delete(synchronize_session=False)
    db.session.commit()


try:
    run()
finally:
    cleanup()
print("\nAll pricing checks passed.")