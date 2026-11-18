"""Bonus months for new subscribers: eligibility, one-time and subscription
payments, the deferred Paystack subscription, and the pages.
PYTHONPATH=. python scripts/bonus_test.py     (scratch database only)"""
import os
import uuid
from datetime import datetime, timedelta

os.environ.pop("MAIL_SERVER", None)
os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_bonus_unit"

from flask import render_template

from app import create_app, set_tenant
from app.extensions import db
from app.billing import paystack, plans, quotes, service
from app.billing.access import utcnow
from app.models.billing import SubscriptionPayment
from app.models.pricing import Bonus
from app.models.tenant import Organization

app = create_app()
app.app_context().push()
RUN = uuid.uuid4().hex[:6]
NOW = utcnow()
inits, subs, verify, made = [], [], {}, []


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def fake_init(**kw):
    inits.append(kw)
    return {"authorization_url": "https://checkout.paystack.test/b", "reference": kw["reference"]}


def fake_subscription(customer, plan_code, authorization, start_date, key=None):
    subs.append({"customer": customer, "plan_code": plan_code, "authorization": authorization, "start": start_date})
    return {"subscription_code": f"SUB_B_{RUN}_{len(subs)}", "email_token": "tok_b"}


paystack.initialize_transaction = fake_init
paystack.verify_transaction = lambda ref, key=None: verify[ref]
paystack.create_plan = lambda **kw: {"plan_code": "PLN_bonus_" + uuid.uuid4().hex[:8]}
paystack.create_subscription = fake_subscription


def new_org(tag):
    name = f"Bonus {tag} {RUN}"
    c = app.test_client()
    r = c.post("/signup", data={"org_name": name, "username": f"b{tag}{RUN}", "full_name": "T",
                                "email": f"b-{tag}-{RUN}@example.test", "password": "password123",
                                "accept_terms": "on"})
    assert r.status_code == 302, "signup failed"
    return c, name


def org(name):
    db.session.expire_all()
    return Organization.query.filter_by(name=name).one()


def checkout(c, mode, interval="annual", expected_bonus="3"):
    q = quotes.quote("starter", interval, "NGN")
    return c.post("/billing/checkout", data={"plan": "starter", "interval": interval, "currency": "NGN",
                                             "email": "fin@bonus.org", "mode": mode,
                                             "expected_total": str(q["total_minor"]),
                                             "expected_bonus": expected_bonus})


def settle(c, reusable=True):
    init = inits[-1]
    ref = init["reference"]
    verify[ref] = {"status": "success", "reference": ref, "amount": init["amount_minor"], "currency": "NGN",
                   "paid_at": utcnow().isoformat() + "Z",
                   "customer": {"customer_code": f"CUS_B_{uuid.uuid4().hex[:8]}", "email": "fin@bonus.org"},
                   "authorization": {"authorization_code": "AUTH_b", "reusable": reusable, "channel": "card"},
                   "metadata": init["metadata"]}
    return c.get(f"/billing/callback?reference={ref}", follow_redirects=True)


def payments(o):
    set_tenant(o.id)
    return SubscriptionPayment.query.filter_by(organization_id=o.id).order_by(SubscriptionPayment.paid_at).all()


def run():
    print("--- Months arithmetic ---")
    check(plans.add_months(datetime(2026, 10, 15), 3) == datetime(2027, 1, 15), "three months on")
    check(plans.add_months(datetime(2026, 11, 30), 3) == datetime(2027, 2, 28), "month ends are clamped")
    check(plans.add_months(datetime(2027, 11, 30, 9, 30), 3) == datetime(2028, 2, 29, 9, 30), "leap years")
    check(plans.add_months(datetime(2026, 12, 31), 12) == datetime(2027, 12, 31), "a whole year")

    print("\n--- Which bonus applies ---")
    b = Bonus(name=f"BF {RUN}", bonus_months=3, interval="annual", starts_at=NOW - timedelta(days=1),
              ends_at=NOW + timedelta(days=1), active=True, created_by="test")
    big = Bonus(name=f"Org {RUN}", bonus_months=6, plan_key="organisation", interval="annual",
                starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1), active=True, created_by="test")
    db.session.add_all([b, big])
    db.session.commit()
    made.extend([b.id, big.id])
    running = quotes.running_bonuses(NOW)
    check(quotes.pick_bonus(running, "starter", "annual", "NGN", NOW)["bonus_months"] == 3, "the annual bonus applies")
    check(quotes.pick_bonus(running, "starter", "monthly", "NGN", NOW) is None, "but not to monthly")
    check(quotes.pick_bonus(running, "organisation", "annual", "NGN", NOW)["bonus_months"] == 6,
          "the bigger applicable bonus wins")
    big.active = False
    db.session.commit()

    print("\n--- Advertising ---")
    r = app.test_client().get("/pricing")
    check(b"get 15" in r.data and b"+3 bonus months free" in r.data, "the pricing page advertises it")
    with app.test_request_context("/"):
        html = render_template("billing/_promo_banner.html")
    check("get 15" in html, "the banner for the landing page advertises it")

    print("\n--- A new subscriber pays once, annually ---")
    cA, nA = new_org("a")
    check(quotes.bonus_for(org(nA), "starter", "annual", "NGN")["bonus_months"] == 3, "a new organisation is eligible")
    r = checkout(cA, "once")
    check(r.status_code == 302 and r.headers["Location"].startswith("https://checkout.paystack.test/") and len(inits) == 1,
          "pay-once goes to Paystack")
    check(inits[-1]["plan_code"] is None and inits[-1]["metadata"]["bonus_months"] == 3, "it carries the bonus months")
    settle(cA)
    oA = org(nA)
    days = (oA.current_period_end - utcnow()).days
    check(454 <= days <= 461, f"12 months plus 3 bonus months ({days} days)")
    pay = payments(oA)[0]
    check(pay.bonus_months == 3 and pay.bonus_name == f"BF {RUN}", "the payment records the bonus")
    check(quotes.bonus_for(org(nA), "starter", "annual", "NGN") is None, "no bonus the second time")
    end1 = oA.current_period_end
    checkout(cA, "once", expected_bonus="0")
    settle(cA)
    end2 = org(nA).current_period_end
    check(365 <= (end2 - end1).days <= 367, "a renewal adds only the year")
    check(payments(org(nA))[1].bonus_months is None, "and records no bonus")

    print("\n--- Guards ---")
    cB, nB = new_org("b")
    n = len(inits)
    r = checkout(cB, "once", expected_bonus="0")
    check(r.status_code == 302 and len(inits) == n, "a changed offer is refused before Paystack is called")
    checkout(cB, "once", interval="monthly", expected_bonus="0")
    settle(cB)
    check(payments(org(nB))[0].bonus_months is None, "no bonus on a monthly payment")

    print("\n--- A new subscriber subscribes by card ---")
    cC, nC = new_org("c")
    checkout(cC, "subscribe")
    init = inits[-1]
    amount_c = init["amount_minor"]
    check(init["plan_code"] is None and init["channels"] == ["card"] and init["metadata"]["mode"] == "subscribe_bonus"
          and init["metadata"]["offer_id"], "a plain card charge now, no plan yet")
    settle(cC)
    oC = org(nC)
    check(len(subs) == 1 and subs[0]["authorization"] == "AUTH_b" and subs[0]["plan_code"].startswith("PLN_bonus_"),
          "the subscription is created with the saved card")
    check(abs((subs[0]["start"] - oC.current_period_end).total_seconds()) < 2,
          "its first debit is when the bonus period ends")
    check(oC.paystack_subscription_code and oC.paystack_subscription_code.startswith("SUB_B_"), "subscription recorded")
    check(454 <= (oC.current_period_end - utcnow()).days <= 461, "access runs 15 months")
    renew = {"status": "success", "reference": f"renew_{RUN}", "amount": amount_c, "currency": "NGN",
             "paid_at": (utcnow() + timedelta(days=458)).isoformat() + "Z",
             "customer": {"customer_code": oC.paystack_customer_code},
             "plan": {"plan_code": subs[0]["plan_code"]}, "metadata": {}}
    service.handle_event("charge.success", renew)
    renewed = [p for p in payments(org(nC)) if p.reference == f"renew_{RUN}"]
    check(len(renewed) == 1 and renewed[0].bonus_months is None, "the renewal charge carries no bonus")

    cD, nD = new_org("d")
    n = len(subs)
    checkout(cD, "subscribe")
    settle(cD, reusable=False)
    oD = org(nD)
    check(len(subs) == n and not oD.paystack_subscription_code and oD.billing_status == "active",
          "a card that can't be saved still gets the bonus, with no automatic renewal")
    check(454 <= (oD.current_period_end - utcnow()).days <= 461, "and the full 15 months")

    print("\n--- When the bonus ends ---")
    b.ends_at = NOW - timedelta(hours=1)
    db.session.commit()
    cE, nE = new_org("e")
    check(quotes.bonus_for(org(nE), "starter", "annual", "NGN") is None, "an ended bonus no longer applies")
    check(quotes.bonus_banner() is None, "and it leaves the pricing page")


try:
    run()
finally:
    db.session.rollback()
    Bonus.query.filter(Bonus.id.in_(made)).delete(synchronize_session=False)
    db.session.commit()
print("\nAll bonus checks passed.")