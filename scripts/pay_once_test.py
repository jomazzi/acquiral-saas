"""Pay-once (no auto-renew) payments, extension, grace and guards.
PYTHONPATH=. python scripts/pay_once_test.py     (scratch database only)"""
import os
import uuid
from datetime import timedelta

os.environ.pop("MAIL_SERVER", None)
os.environ["PAYSTACK_SECRET_KEY"] = "sk_test_pay_once_unit"

from app import create_app, set_tenant
from app.extensions import db
from app.billing import paystack, quotes, service
from app.billing.access import access_state, utcnow
from app.models.billing import SubscriptionPayment
from app.models.pricing import TaxSetting
from app.models.tenant import Organization

app = create_app()
app.app_context().push()
RUN = uuid.uuid4().hex[:6]
inits, verify = [], {}
paystack.initialize_transaction = lambda **kw: (
    inits.append(kw), {"authorization_url": "https://checkout.paystack.test/once", "reference": kw["reference"]})[1]
paystack.verify_transaction = lambda ref, key=None: verify[ref]
paystack.create_plan = lambda **kw: {"plan_code": "PLN_never_used"}


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


ORG = f"Once Org {RUN}"
c = app.test_client()
r = c.post("/signup", data={"org_name": ORG, "username": f"o{RUN}", "full_name": "T",
                            "email": f"once-{RUN}@example.test", "password": "password123", "accept_terms": "on"})
check(r.status_code == 302, "test organisation signed up")


def org():
    db.session.expire_all()
    return Organization.query.filter_by(name=ORG).one()


def checkout(mode, interval="monthly", currency="NGN", plan="starter"):
    q = quotes.quote(plan, interval, currency)
    r = c.post("/billing/checkout", data={"plan": plan, "interval": interval, "currency": currency,
                                          "email": "fin@once.org", "mode": mode,
                                          "expected_total": str(q["total_minor"])})
    return r, q


def settle(status="success"):
    init = inits[-1]
    ref = init["reference"]
    verify[ref] = {"status": status, "reference": ref, "amount": init["amount_minor"], "currency": init["currency"],
                   "paid_at": utcnow().isoformat() + "Z",
                   "customer": {"customer_code": f"CUS_ONCE_{RUN}", "email": "fin@once.org"},
                   "metadata": init["metadata"]}
    return c.get(f"/billing/callback?reference={ref}", follow_redirects=True)


print("--- A one-time payment ---")
r, q = checkout("once")
check(r.status_code == 302 and r.headers["Location"].startswith("https://checkout.paystack.test/") and len(inits) == 1,
      "pay-once goes to Paystack")
check(inits[-1]["plan_code"] is None and inits[-1]["amount_minor"] == q["total_minor"]
      and inits[-1]["metadata"]["mode"] == "once",
      "it is a plain transaction (no plan, so OPay and transfers are offered) for the quoted total")
r = settle()
o = org()
check(b"paid up until" in r.data, "the customer is told when they are paid until")
check(o.billing_status == "active" and not o.paystack_subscription_code, "active, with no subscription behind it")
end1 = o.current_period_end
check(30 <= (end1 - utcnow()).days <= 31, "one month is added")
set_tenant(o.id)
p = SubscriptionPayment.query.filter_by(organization_id=o.id).one()
check(p.amount_minor == q["total_minor"] and p.list_minor == q["list_minor"] and p.vat_minor == q["vat_minor"]
      and p.offer_id is None, "the payment stores its price breakdown")

print("\n--- Paying again before it ends ---")
r, q = checkout("once")
settle()
end2 = org().current_period_end
check(30 <= (end2 - end1).days <= 31, "the next month is added after the current end, not from today")
r, _ = checkout("subscribe")
check(r.status_code == 302 and "/billing" in r.headers["Location"] and len(inits) == 2,
      "subscribing is refused while prepaid (no new Paystack call)")

print("\n--- Slow payment methods ---")
r, q = checkout("once")
before = org().current_period_end
r = settle("pending")
check(b"waiting for your bank" in r.data and org().current_period_end == before, "a pending bank payment waits")
service.handle_event("charge.success", {**verify[inits[-1]["reference"]], "status": "success"})
check(org().current_period_end > before, "the signed webhook completes it later")

print("\n--- After it lapses ---")
o = org()
o.current_period_end = utcnow() - timedelta(days=10)
db.session.commit()
check(access_state(org()) == "expired", "past the end and the grace period it is expired")
r, q = checkout("once")
settle()
check((org().current_period_end - utcnow()).days >= 30, "a new period starts from the payment date")

print("\n--- Guards ---")
o = org()
o.paystack_subscription_code, o.paystack_email_token = f"SUB_{RUN}", "tok"
o.billing_status, o.current_period_end = "active", utcnow() + timedelta(days=10)
db.session.commit()
n = len(inits)
r, _ = checkout("once")
check(r.status_code == 302 and len(inits) == n, "pay-once is refused while a recurring subscription is live")
o = org()
o.paystack_subscription_code = o.paystack_email_token = None
db.session.commit()

t = db.session.get(TaxSetting, "USD")
t.sellable = False
db.session.commit()
n = len(inits)
r, _ = checkout("once", currency="USD")
check(r.status_code == 302 and len(inits) == n, "a currency that is switched off can't be bought")
check(b'data-currency="USD"' not in app.test_client().get("/pricing").data, "and it disappears from the pricing page")
t.sellable = True
db.session.commit()

print("\nAll pay-once checks passed.")