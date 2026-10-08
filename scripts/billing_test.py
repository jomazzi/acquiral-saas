"""
Functional test for Paystack subscription billing, via the Flask test
client against the real dev Postgres (so row-level security is in play
exactly as in production). Paystack's HTTP API is mocked; everything
else -- signup, trial, checkout, callback, signed webhooks, renewals,
cancellation, lapse to read-only, user limits, tenant isolation -- runs
for real.

    PYTHONPATH=. python scripts/billing_test.py

Safe to re-run: every organisation it creates has a unique name.
"""
import hashlib
import hmac
import json
import os
import uuid
from datetime import timedelta

os.environ.pop("PAYSTACK_SECRET_KEY", None)

from app import create_app, set_tenant
from app.extensions import db
from app.models.tenant import Organization, User
from app.models.billing import SubscriptionPayment
from app.billing import paystack, plans
from app.billing.access import access_state, utcnow

app = create_app()
SECRET = "sk_test_unit_test_key"
RUN = uuid.uuid4().hex[:6]
CUS_A, SUB_A, TOK_A = f"CUS_A_{RUN}", f"SUB_A_{RUN}", f"tok_A_{RUN}"  # unique per run: codes are looked up globally


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def signup(client, org_name, username="admin"):
    r = client.post("/signup", data={"org_name": org_name, "username": username,
                                      "full_name": "Test Admin", "password": "password123"},
                    follow_redirects=True)
    assert r.status_code == 200
    return r


def get_org(name):
    with app.app_context():
        return Organization.query.filter_by(name=name).one()


def update_org(org_id, **fields):
    with app.app_context():
        org = db.session.get(Organization, org_id)
        for k, v in fields.items():
            setattr(org, k, v)
        db.session.commit()


def refresh(org_id):
    with app.app_context():
        org = db.session.get(Organization, org_id)
        db.session.expunge(org)
        return org


def payments_for(org_id):
    with app.app_context():
        set_tenant(org_id)
        rows = SubscriptionPayment.query.order_by(SubscriptionPayment.paid_at).all()
        db.session.rollback()
        return rows


def webhook(client, event, data, secret=SECRET, signature=None):
    body = json.dumps({"event": event, "data": data}).encode()
    sig = signature if signature is not None else hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/billing/webhook/paystack", data=body,
                       headers={"X-Paystack-Signature": sig, "Content-Type": "application/json"})


# ---------------------------------------------------------------- mocks
calls = {"init": [], "verify": [], "disable": []}
next_verify = {}


def fake_initialize(email, amount_kobo, plan_code, reference, callback_url, metadata):
    calls["init"].append(dict(email=email, amount_kobo=amount_kobo, plan_code=plan_code,
                              reference=reference, callback_url=callback_url, metadata=metadata))
    return {"authorization_url": f"https://checkout.paystack.test/{reference}", "reference": reference}


def fake_verify(reference):
    calls["verify"].append(reference)
    return next_verify[reference]


def fake_disable(code, token):
    calls["disable"].append((code, token))
    return {}


paystack.initialize_transaction = fake_initialize
paystack.verify_transaction = fake_verify
paystack.disable_subscription = fake_disable

# ===================================================================
print("\n--- Without Paystack configured: billing must stay out of the way ---")
c0 = app.test_client()
name0 = f"NoPay Org {RUN}"
signup(c0, name0)
org0 = get_org(name0)
check(org0.billing_status == "trialing" and org0.trial_ends_at is not None, "signup starts a trial")
days = (org0.trial_ends_at - utcnow()).days
check(plans.TRIAL_DAYS - 1 <= days <= plans.TRIAL_DAYS, f"trial is {plans.TRIAL_DAYS} days long")
update_org(org0.id, trial_ends_at=utcnow() - timedelta(days=30))   # long-expired trial
r = c0.post("/funders", data={"name": "StillWorks", "notes": ""}, follow_redirects=True)
check(b"Funder added" in r.data, "expired trial does NOT block writes when Paystack isn't configured")
check(app.test_client().get("/pricing").status_code == 200, "public /pricing renders")

# ===================================================================
os.environ["PAYSTACK_SECRET_KEY"] = SECRET
for k in plans.PLANS:
    for i in plans.INTERVALS:
        os.environ[plans.plan_code_env_var(k, i)] = f"PLN_{k}_{i}"

print("\n--- Trial, pricing and billing page ---")
c = app.test_client()
nameA = f"Billing Org A {RUN}"
signup(c, nameA)
orgA = get_org(nameA)
r = app.test_client().get("/pricing")
check(b"Starter" in r.data and b"Organisation" in r.data and b"15,000" in r.data, "pricing page shows both plans and naira prices")
r = c.get("/billing")
check(r.status_code == 200 and b"Free trial" in r.data, "billing page shows the trial to an admin")
r = c.get("/")
r = c.get("/accounts")
check(b"Free trial:" in r.data, "trial banner shown inside the app")

print("\n--- Checkout ---")
r = c.post("/billing/checkout", data={"plan": "starter", "interval": "monthly", "email": "not-an-email"}, follow_redirects=True)
check(b"valid billing email" in r.data and not calls["init"], "invalid email is rejected before calling Paystack")
r = c.post("/billing/checkout", data={"plan": "bogus", "interval": "monthly", "email": "fin@a.org"}, follow_redirects=True)
check(not calls["init"], "unknown plan is rejected")
r = c.post("/billing/checkout", data={"plan": "starter", "interval": "monthly", "email": "fin@a.org"})
check(r.status_code == 302 and r.headers["Location"].startswith("https://checkout.paystack.test/"), "checkout redirects to Paystack")
init = calls["init"][-1]
check(init["amount_kobo"] == 15_000 * 100 and init["plan_code"] == "PLN_starter_monthly", "charged the Starter monthly plan amount in kobo")
check(init["metadata"]["organization_id"] == str(orgA.id), "checkout metadata carries the organisation id")
check(init["callback_url"].endswith("/billing/callback"), "callback URL points back at us")
check(refresh(orgA.id).billing_email == "fin@a.org", "billing email saved")

print("\n--- Callback (customer returns from Paystack) ---")
ref = init["reference"]
next_verify[ref] = {"status": "success", "reference": ref, "amount": 1_500_000, "currency": "NGN",
                    "paid_at": utcnow().isoformat() + "Z",
                    "customer": {"customer_code": CUS_A, "email": "fin@a.org"},
                    "metadata": {"organization_id": str(orgA.id), "plan_key": "starter", "interval": "monthly"}}
r = c.get(f"/billing/callback?reference={ref}", follow_redirects=True)
check(b"Payment received" in r.data, "callback confirms payment")
o = refresh(orgA.id)
check(o.billing_status == "active" and o.plan_key == "starter" and o.plan_interval == "monthly", "org is active on Starter monthly")
check(o.current_period_end and (o.current_period_end - utcnow()).days >= 29, "period end set about a month ahead")
check(o.paystack_customer_code == CUS_A, "customer code stored")
check(len(payments_for(orgA.id)) == 1, "one payment recorded")
c.get(f"/billing/callback?reference={ref}", follow_redirects=True)
check(len(payments_for(orgA.id)) == 1, "replaying the callback does not double-record")
r = c.get("/billing")
check(b"Active" in r.data and b"15,000.00" in r.data, "billing page shows active plan and the payment")

print("\n--- Tenant isolation on callback ---")
cB = app.test_client()
nameB = f"Billing Org B {RUN}"
signup(cB, nameB)
orgB = get_org(nameB)
r = cB.get(f"/billing/callback?reference={ref}")
check(r.status_code == 403, "org B cannot claim org A's payment reference")
check(refresh(orgB.id).billing_status == "trialing", "org B untouched")
check(b"acq_" not in cB.get("/billing").data, "org B's billing history doesn't show org A's payment (RLS)")
next_verify[f"failed-ref-{RUN}"] = {"status": "failed", "metadata": {"organization_id": str(orgB.id)}}
r = cB.get(f"/billing/callback?reference=failed-ref-{RUN}", follow_redirects=True)
check(b"wasn&#39;t completed" in r.data and refresh(orgB.id).billing_status == "trialing", "failed payment doesn't activate")

print("\n--- Webhook security ---")
check(webhook(c, "charge.success", {}, signature="deadbeef").status_code == 401, "bad signature rejected")
check(webhook(c, "charge.success", {}, signature="").status_code == 401, "missing signature rejected")
check(webhook(c, "charge.success", {}, secret="wrong-key").status_code == 401, "signature from the wrong key rejected")
check(webhook(c, "charge.success", {"reference": "x", "status": "success"}).status_code == 200, "valid signature + unknown org is acknowledged and ignored")
check(webhook(c, "something.else", {"metadata": {"organization_id": str(orgA.id)}}).status_code == 200, "unhandled event types are acknowledged")

print("\n--- Webhook: subscription linking and renewal ---")
r = webhook(c, "subscription.create", {
    "subscription_code": SUB_A, "email_token": TOK_A,
    "customer": {"customer_code": CUS_A, "email": "fin@a.org"},
    "next_payment_date": (utcnow() + timedelta(days=30)).isoformat() + "Z"})
o = refresh(orgA.id)
check(r.status_code == 200 and o.paystack_subscription_code == SUB_A and o.paystack_email_token == TOK_A, "subscription.create stores code and token")

renew_ref = f"renewal-1-{RUN}"
renew = {"status": "success", "reference": renew_ref, "amount": 1_500_000, "currency": "NGN",
         "paid_at": (utcnow() + timedelta(days=30)).isoformat() + "Z",
         "customer": {"customer_code": CUS_A, "email": "fin@a.org"},
         "plan": {"plan_code": "PLN_starter_monthly"}, "metadata": {}}
before_end = refresh(orgA.id).current_period_end
webhook(c, "charge.success", renew)
o = refresh(orgA.id)
check(len(payments_for(orgA.id)) == 2, "renewal recorded (found org by customer code, plan by plan code)")
check(o.current_period_end > before_end, "renewal extends the period")
webhook(c, "charge.success", renew)
check(len(payments_for(orgA.id)) == 2, "duplicate webhook delivery is idempotent")
check(len(payments_for(orgB.id)) == 0, "org B has no payments")

print("\n--- Past due, then recovered ---")
webhook(c, "invoice.payment_failed", {"subscription": {"subscription_code": SUB_A}, "customer": {"customer_code": CUS_A}})
check(refresh(orgA.id).billing_status == "past_due" and access_state(refresh(orgA.id)) == "past_due", "failed renewal marks org past due")
r = c.post("/funders", data={"name": "StillWritableWhenPastDue", "notes": ""}, follow_redirects=True)
check(b"Funder added" in r.data, "past-due org keeps write access during the grace period")
webhook(c, "charge.success", {**renew, "reference": f"renewal-2-{RUN}", "paid_at": (utcnow() + timedelta(days=31)).isoformat() + "Z"})
check(refresh(orgA.id).billing_status == "active", "a successful charge restores active")

print("\n--- Plan change guard ---")
r = c.post("/billing/checkout", data={"plan": "organisation", "interval": "annual", "email": "fin@a.org"}, follow_redirects=True)
check(b"already have a live subscription" in r.data, "can't start a second subscription while one is live (no double billing)")

print("\n--- User limits (Starter = 3 users) ---")
for i in range(2):
    r = c.post("/users", data={"username": f"staff{i}", "full_name": "S", "role": "user", "password": "password123"}, follow_redirects=True)
    check(b"created" in r.data, f"added user {i + 2} of 3")
r = c.post("/users", data={"username": "staff-over", "full_name": "S", "role": "user", "password": "password123"}, follow_redirects=True)
check(b"allows up to 3 active users" in r.data, "4th user blocked on Starter")

print("\n--- Cancel ---")
r = c.post("/billing/cancel", follow_redirects=True)
check(calls["disable"] == [(SUB_A, TOK_A)] and b"Subscription cancelled" in r.data, "cancel calls Paystack with the subscription code and token")
check(access_state(refresh(orgA.id)) == "canceling", "cancelled org keeps access until period end")
r = c.post("/funders", data={"name": "WritableUntilPeriodEnd", "notes": ""}, follow_redirects=True)
check(b"Funder added" in r.data, "still writable after cancelling")
webhook(c, "subscription.not_renew", {"subscription_code": SUB_A, "customer": {"customer_code": CUS_A}})
check(refresh(orgA.id).billing_status == "canceled", "not_renew webhook keeps it cancelled")

print("\n--- Lapse to read-only (never locked out) ---")
update_org(orgA.id, current_period_end=utcnow() - timedelta(days=10))
check(access_state(refresh(orgA.id)) == "expired", "past period end + grace = expired")
r = c.post("/funders", data={"name": "ShouldBeBlocked", "notes": ""}, follow_redirects=True)
check(b"read-only" in r.data and b"ShouldBeBlocked" not in c.get("/funders").data, "writes are blocked when expired")
r = c.get("/funders")
check(r.status_code == 200 and b"read-only" in r.data, "reads still work, with a banner")
check(c.get("/billing").status_code == 200, "billing page still reachable to re-subscribe")
r = c.post("/billing/checkout", data={"plan": "organisation", "interval": "annual", "email": "fin@a.org"})
check(r.status_code == 302 and "checkout.paystack.test" in r.headers["Location"], "an expired org can start a new checkout")
check(calls["init"][-1]["amount_kobo"] == 450_000 * 100, "Organisation annual charged at the right amount")

print("\n--- Expired trial blocks writes; comped & demo orgs never do ---")
update_org(orgB.id, trial_ends_at=utcnow() - timedelta(days=1))
r = cB.post("/funders", data={"name": "TrialOver", "notes": ""}, follow_redirects=True)
check(b"read-only" in r.data, "expired trial is read-only")
update_org(orgB.id, billing_status="comped")
r = cB.post("/funders", data={"name": "CompedOK", "notes": ""}, follow_redirects=True)
check(b"Funder added" in r.data, "comped org is unaffected")

# Non-admins can't reach billing
with app.app_context():
    u = User(organization_id=orgB.id, username="plainstaff", role="user")
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
cs = app.test_client()
cs.post("/login", data={"username": "plainstaff", "password": "password123"})
check(cs.get("/billing").status_code == 403, "non-admin staff can't open the billing page")

print("\nAll billing checks passed.")
