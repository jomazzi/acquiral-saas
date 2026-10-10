"""Functional test for invoice Pay Now (public link, org's own Paystack,
transfer claims, settlement, tenant isolation). Paystack HTTP is mocked.

    PYTHONPATH=. python scripts/pay_now_test.py
"""
import hashlib, hmac, json, os, re, uuid
from datetime import date
from types import SimpleNamespace

os.environ.pop("PAYSTACK_SECRET_KEY", None)

from app import create_app, set_tenant
from app.extensions import db
from app.models.tenant import Organization
from app.models.accounting import Account, JournalEntry
from app.models.invoicing import Customer, Invoice, InvoiceLine
from app.models.payments import InvoicePayment, InvoiceShareLink, PaymentSettings
from app.billing import paystack
from app.payments import crypto, service

app = create_app()
RUN = uuid.uuid4().hex[:6]
KEY = "sk_test_abcdefghij1234"


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def signup(client, name):
    r = client.post("/signup", data={"org_name": name, "username": "admin", "full_name": "T Admin",
                                      "password": "password123"}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        return Organization.query.filter_by(name=name).one().id


calls = {"init": [], "verify": []}
verify_data = {}


def fake_init(email, amount_minor, currency, plan_code, reference, callback_url, metadata, key=None):
    calls["init"].append(dict(email=email, amount_minor=amount_minor, currency=currency, plan_code=plan_code,
                              reference=reference, metadata=metadata, key=key))
    return {"authorization_url": f"https://checkout.test/{reference}"}


def fake_verify(reference, key=None):
    calls["verify"].append((reference, key))
    return verify_data[reference]


paystack.initialize_transaction = fake_init
paystack.verify_transaction = fake_verify
paystack.check_key = lambda key: {} if key == KEY else (_ for _ in ()).throw(paystack.PaystackError("bad"))


def make_invoice(org_id, number, qty=2, price=10000.0):
    """Creates + sends an invoice through the real send route's logic via test client."""
    with app.app_context():
        set_tenant(org_id)
        cust = Customer(organization_id=org_id, name=f"Cust {number}")
        db.session.add(cust); db.session.flush()
        inv = Invoice(organization_id=org_id, invoice_number=number, customer_id=cust.id,
                      issue_date=date.today(), vat_rate=7.5, status="draft",
                      lines=[InvoiceLine(organization_id=org_id, description="Consulting", quantity=qty, unit_price=price)])
        db.session.add(inv); db.session.commit()
        return inv.id


def invoice_state(org_id, inv_id):
    with app.app_context():
        set_tenant(org_id)
        inv = Invoice.query.filter_by(id=inv_id).one()
        out = SimpleNamespace(status=inv.status, total=inv.total, paid_je=inv.payment_journal_entry_id)
        db.session.rollback()
        return out


def payments(org_id, inv_id):
    with app.app_context():
        set_tenant(org_id)
        rows = [SimpleNamespace(status=p.status, method=p.method, ref=p.reference, id=p.id)
                for p in InvoicePayment.query.filter_by(invoice_id=inv_id).all()]
        db.session.rollback()
        return rows


def bank_id(org_id):
    with app.app_context():
        set_tenant(org_id)
        b = Account.query.filter_by(is_cash_or_bank=True, currency="NGN").order_by(Account.code).first()
        bid = b.id if b else None
        db.session.rollback()
        return bid


def token_for(org_id, inv_id):
    with app.app_context():
        set_tenant(org_id)
        inv = Invoice.query.filter_by(id=inv_id).one()
        t = service.ensure_share_link(inv).token
        db.session.rollback()
        return t


def wh_post(client, org_id, event, data, key=KEY, sig=None):
    body = json.dumps({"event": event, "data": data}).encode()
    s = sig if sig is not None else hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return client.post(f"/pay/webhook/{org_id}", data=body,
                       headers={"X-Paystack-Signature": s, "Content-Type": "application/json"})


print("\n--- setup ---")
admin = app.test_client()
org = signup(admin, f"PayNow Org A {RUN}")
other = app.test_client()
org_b = signup(other, f"PayNow Org B {RUN}")
bank = bank_id(org)
check(bank is not None, "seeded NGN bank account exists")

inv_id = make_invoice(org, f"INV-PN-{RUN}-1")
inv_draft = make_invoice(org, f"INV-PN-{RUN}-D")
r = admin.post(f"/invoices/{inv_id}/send")
check(r.status_code in (302, 200), "invoice sent through the real route")
total = invoice_state(org, inv_id).total
check(total == 21500.0, f"invoice total 21,500 (got {total})")

print("\n--- invoice detail exposes the link ---")
r = admin.get(f"/invoices/{inv_id}")
m = re.search(r'id="pay-url" value="([^"]+)"', r.get_data(as_text=True))
check(m is not None, "invoice page shows a payment link")
token = m.group(1).rsplit("/", 1)[1]
r = admin.get(f"/invoices/{inv_id}/pdf")
check(r.status_code == 200 and r.data[:4] == b"%PDF", "PDF still renders with pay link")

print("\n--- public page (no login) ---")
pub = app.test_client()
r = pub.get(f"/i/{token}")
html = r.get_data(as_text=True)
check(r.status_code == 200 and f"INV-PN-{RUN}-1" in html, "public page shows the invoice, no login")
check("21,500.00" in html, "public page shows the total")
check("noindex" in html and r.headers.get("Cache-Control") == "no-store", "public page is noindex + no-store")
check("Pay by card" not in html and "bank transfer" not in html, "no payment options before org configures anything")
check(pub.get("/i/not-a-real-token").status_code == 404, "unknown token -> 404")
check(pub.get("/i/" + "x" * 200).status_code == 404, "oversized token -> 404")
tok_draft = token_for(org, inv_draft)
check(pub.get(f"/i/{tok_draft}").status_code == 404, "draft invoice link is not public")

print("\n--- admin settings ---")
check(admin.get("/settings/payments").status_code == 200, "settings page loads")
r = admin.post("/settings/payments", data={"paystack_key": "nonsense", "bank_account_id": str(bank)}, follow_redirects=True)
check("look like a Paystack secret key" in r.get_data(as_text=True), "malformed key rejected")
r = admin.post("/settings/payments", data={"paystack_key": "sk_test_wrongwrongwrong", "bank_account_id": str(bank)}, follow_redirects=True)
check("Paystack rejected that key" in r.get_data(as_text=True), "key Paystack rejects is not saved")
r = admin.post("/settings/payments", data={"paystack_key": KEY, "bank_account_id": str(bank),
                                           "show_bank_details": "on", "bank_instructions": "Use invoice no. as narration"},
               follow_redirects=True)
check("Payment settings saved" in r.get_data(as_text=True), "valid key + bank saved")
with app.app_context():
    set_tenant(org)
    ps = PaymentSettings.query.one()
    check(KEY not in ps.paystack_secret_enc and crypto.decrypt(ps.paystack_secret_enc) == KEY, "key stored encrypted, round-trips")
    check(ps.paystack_key_hint == "sk_test_…1234", "only a hint is kept in plain text")
    db.session.rollback()
check(KEY not in admin.get("/settings/payments").get_data(as_text=True), "key never rendered back to the browser")
check(KEY not in admin.get(f"/invoices/{inv_id}").get_data(as_text=True), "key not on invoice page")

print("\n--- card payment ---")
html = pub.get(f"/i/{token}").get_data(as_text=True)
check("Pay ₦21,500.00" in html and "Account number" in html or "bank transfer" in html, "page now offers card + bank transfer")
r = pub.post(f"/i/{token}/card", data={"email": "bad"})
check("r=bademail" in r.headers["Location"], "bad email rejected")
r = pub.post(f"/i/{token}/card", data={"email": "payer@example.com"})
init = calls["init"][-1]
check(r.status_code == 302 and r.headers["Location"].startswith("https://checkout.test/"), "redirects to Paystack checkout")
check(init["amount_minor"] == 2150000 and init["currency"] == "NGN" and init["plan_code"] is None, "amount from server-side invoice total (kobo), no plan")
check(init["key"] == KEY, "checkout created with the ORG's key, not the platform's")
ref = init["reference"]

# wrong-invoice metadata on return -> 403
verify_data[ref] = {"reference": ref, "status": "success", "amount": 2150000, "currency": "NGN",
                    "metadata": {"invoice_id": str(uuid.uuid4())}, "customer": {"email": "payer@example.com"}}
check(pub.get(f"/i/{token}/return?reference={ref}").status_code == 403, "return with mismatched invoice metadata refused")
check(invoice_state(org, inv_id).status == "sent", "...and nothing was posted")

verify_data[ref] = {"reference": ref, "status": "success", "amount": 2150000, "currency": "NGN",
                    "metadata": {"invoice_id": str(inv_id)}, "customer": {"email": "payer@example.com"}}
r = pub.get(f"/i/{token}/return?reference={ref}")
check("r=paid" in r.headers["Location"], "return verifies and settles")
st = invoice_state(org, inv_id)
check(st.status == "paid" and st.paid_je is not None, "invoice marked paid with a journal entry")
check(calls["verify"][-1][1] == KEY, "verification used the org's key")
with app.app_context():
    set_tenant(org)
    je = JournalEntry.query.filter_by(id=st.paid_je).one()
    dr = sum(l.debit for l in je.lines); cr = sum(l.credit for l in je.lines)
    check(abs(dr - 21500) < 0.01 and abs(cr - 21500) < 0.01, "entry balances: Dr bank / Cr AR 21,500")
    check(any(l.account_id == bank and l.debit > 0 for l in je.lines), "debit hit the configured bank account")
    db.session.rollback()

# webhook after callback = idempotent
cnt_before = len(payments(org, inv_id))
wh_data = dict(verify_data[ref])
r = wh_post(pub, org, "charge.success", wh_data)
check(r.status_code == 200 and len(payments(org, inv_id)) == cnt_before, "webhook after callback is idempotent")

print("\n--- webhook security & edge cases ---")
check(wh_post(pub, org, "charge.success", wh_data, sig="deadbeef").status_code == 401, "bad signature -> 401")
check(wh_post(pub, org, "charge.success", wh_data, key="sk_test_someoneelse1234").status_code == 401, "signed with another key -> 401")
check(wh_post(pub, org_b, "charge.success", wh_data).status_code == 401, "org A's key can't post to org B's webhook")
check(wh_post(pub, uuid.uuid4(), "charge.success", wh_data).status_code == 401, "unknown org -> 401")

# duplicate payment on an already-paid invoice
dup = {"reference": f"dup_{RUN}", "status": "success", "amount": 2150000, "currency": "NGN",
       "metadata": {"invoice_id": str(inv_id)}, "customer": {"email": "x@y.z"}}
check(wh_post(pub, org, "charge.success", dup).status_code == 200, "duplicate payment accepted by webhook")
check(any(p.status == "duplicate" for p in payments(org, inv_id)), "...recorded as 'duplicate', flagged for refund")
je_count = None
with app.app_context():
    set_tenant(org)
    je_count = JournalEntry.query.filter_by(source="invoice_payment").count()
    db.session.rollback()
check(je_count == 1, "...and NOT posted to the books a second time")

# amount mismatch on a fresh invoice
inv2 = make_invoice(org, f"INV-PN-{RUN}-2"); admin.post(f"/invoices/{inv2}/send")
short = {"reference": f"short_{RUN}", "status": "success", "amount": 100000, "currency": "NGN",
         "metadata": {"invoice_id": str(inv2)}, "customer": {"email": "x@y.z"}}
wh_post(pub, org, "charge.success", short)
check(invoice_state(org, inv2).status == "sent" and any(p.status == "mismatch" for p in payments(org, inv2)),
      "under-payment recorded as 'mismatch', invoice stays unpaid")
html = admin.get(f"/invoices/{inv2}").get_data(as_text=True)
check("did not match the invoice" in html, "admin sees the mismatch warning")

# cross-tenant: org A's signed webhook cannot touch org B's invoice
invb = make_invoice(org_b, f"INV-PN-{RUN}-B"); other.post(f"/invoices/{invb}/send")
evil = {"reference": f"evil_{RUN}", "status": "success", "amount": 2150000, "currency": "NGN",
        "metadata": {"invoice_id": str(invb)}, "customer": {"email": "x@y.z"}}
wh_post(pub, org, "charge.success", evil)
check(invoice_state(org_b, invb).status == "sent", "org A's webhook cannot settle org B's invoice")

# unrelated / non-invoice events
check(wh_post(pub, org, "charge.success", {"reference": "r", "status": "success", "metadata": {}}).status_code == 200,
      "charge without invoice metadata ignored with 200")
check(wh_post(pub, org, "transfer.success", {}).status_code == 200, "other events ignored with 200")

print("\n--- bank transfer claims ---")
tok2 = token_for(org, inv2)
r = pub.post(f"/i/{tok2}/claim", data={"name": "", "note": ""})
check("r=noname" in r.headers["Location"], "claim needs a name")
for i in range(7):
    pub.post(f"/i/{tok2}/claim", data={"name": f"Ada {i}", "note": "GTB, 10:30"})
claims = [p for p in payments(org, inv2) if p.status == "claimed"]
check(len(claims) == 5, f"claims capped at 5 open per invoice (got {len(claims)})")
check(invoice_state(org, inv2).status == "sent", "a claim alone never marks the invoice paid")
html = admin.get(f"/invoices/{inv2}").get_data(as_text=True)
check("says they've paid by transfer" in html, "admin sees the pending claim")
pid = claims[0].id
r = admin.post(f"/invoices/{inv2}/claims/{pid}/confirm", data={"bank_account_id": str(bank)}, follow_redirects=True)
check("marked as paid" in r.get_data(as_text=True), "admin confirms claim")
st = invoice_state(org, inv2)
check(st.status == "paid" and st.paid_je is not None, "confirmation posts to the books and marks paid")
r = admin.post(f"/invoices/{inv2}/claims/{claims[1].id}/confirm", data={"bank_account_id": str(bank)}, follow_redirects=True)
check("no longer be marked paid" in r.get_data(as_text=True), "second claim can't double-post")
check(other.post(f"/invoices/{inv2}/claims/{claims[2].id}/dismiss").status_code == 404, "other tenant can't act on these claims")

inv3 = make_invoice(org, f"INV-PN-{RUN}-3"); admin.post(f"/invoices/{inv3}/send")
tok3 = token_for(org, inv3)
pub.post(f"/i/{tok3}/claim", data={"name": "Bola"})
pid3 = [p for p in payments(org, inv3) if p.status == "claimed"][0].id
admin.post(f"/invoices/{inv3}/claims/{pid3}/dismiss")
check(invoice_state(org, inv3).status == "sent" and payments(org, inv3)[0].status == "dismissed", "dismissed claim leaves invoice unpaid")

print("\n--- paid / org without card ---")
check("This invoice has been paid" in pub.get(f"/i/{token}").get_data(as_text=True), "paid invoice page says so, no pay buttons")
tokb = token_for(org_b, invb)
html = pub.get(f"/i/{tokb}").get_data(as_text=True)
check("Pay by card" not in html and "contact" in html.lower(), "org B (nothing configured) shows no payment options")
r = pub.post(f"/i/{tokb}/card", data={"email": "a@b.co"})
check(not r.headers["Location"].startswith("https://checkout"), "card checkout refused when org hasn't connected Paystack")

print("\n--- remove key ---")
admin.post("/settings/payments", data={"action": "remove_key"})
tok_a2 = token_for(org, inv3)
check("Pay by card" not in pub.get(f"/i/{tok_a2}").get_data(as_text=True), "card option disappears once key removed")

print("\n--- RLS ---")
with app.app_context():
    set_tenant(org_b)
    check(InvoicePayment.query.count() == 0 and PaymentSettings.query.count() == 0,
          "org B sees none of org A's payments/settings under RLS")
    db.session.rollback()

print("\nALL PAY NOW CHECKS PASSED")
