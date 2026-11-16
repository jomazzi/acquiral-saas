"""Ops console: hostname gating, password + 2FA login, lockout, replay
protection, read-only views, and the privacy guarantee (the console's
database role cannot read customer data).
    PYTHONPATH=. python scripts/ops_test.py"""
import os
import re
import time
import uuid

import psycopg2
import psycopg2.errors
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta

os.environ.pop("PAYSTACK_SECRET_KEY", None)
os.environ.pop("MAIL_SERVER", None)
for key in ("OPS_HOST", "OPS_DATABASE_URL", "FIELD_ENCRYPTION_KEY", "DATABASE_URL"):
    assert os.environ.get(key), f"{key} must be set in the environment"

from app import create_app
from app.blueprints.ops import totp
from app.blueprints.ops.security import enc

app = create_app()
HOST = os.environ["OPS_HOST"].strip().lower()
BASE = f"http://{HOST}"
RUN = uuid.uuid4().hex[:6]
PW = "a-long-test-password-1"


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def get(c, path):
    return c.get(path, base_url=BASE)


def post(c, path, data, token):
    return c.post(path, data={**data, "csrf": token}, base_url=BASE)


def csrf_from(r):
    m = re.search(rb'name="csrf" value="([^"]+)"', r.data)
    assert m, "no csrf field on page"
    return m.group(1).decode()


def dbc(env):
    return psycopg2.connect(os.environ[env])


def make_admin(username):
    secret = totp.new_secret()
    with dbc("DATABASE_URL") as conn, conn.cursor() as cur:
        cur.execute("insert into ops_admins (username, password_hash, totp_secret_enc) values (%s,%s,%s)",
                    (username, generate_password_hash(PW), enc(secret)))
    return secret


def full_login(c, username, password, secret, step=None):
    tok = csrf_from(get(c, "/ops/login"))
    r = post(c, "/ops/login", {"username": username, "password": password}, tok)
    if r.status_code != 302:
        return r
    tok = csrf_from(get(c, "/ops/2fa"))
    code = totp._code(secret, step if step is not None else int(time.time() // 30))
    return post(c, "/ops/2fa", {"code": code}, tok)


print("--- TOTP ---")
check(totp._code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 1) == "287082", "matches the RFC 6238 test vector")

print("\n--- Hostname gating ---")
plain = app.test_client()
check(plain.get("/ops/login").status_code == 404, "/ops/login is a 404 on the normal site")
c = app.test_client()
check(get(c, "/ops/login").status_code == 200, "login page renders on the ops host")
r = get(c, "/ops/")
check(r.status_code == 302 and "/ops/login" in r.headers["Location"], "dashboard needs login")

print("\n--- Setup: a customer organisation with a payment ---")
ORG = f"Ops Org {RUN}"
tc = app.test_client()
r = tc.post("/signup", data={"org_name": ORG, "username": f"u{RUN}", "full_name": "T", "email": f"ops-{RUN}@example.test",
                             "password": "password123", "accept_terms": "on"})
check(r.status_code == 302, "customer signup works")
with dbc("DATABASE_URL") as conn, conn.cursor() as cur:
    cur.execute("select id::text from organizations where name=%s", (ORG,))
    ORG_ID = cur.fetchone()[0]
with dbc("DATABASE_URL") as conn, conn.cursor() as cur:
    cur.execute("select set_config('app.current_tenant', %s, true)", (ORG_ID,))
    cur.execute("insert into subscription_payments (id, reference, amount_minor, currency, plan_key, plan_interval, paid_at, organization_id) "
                "values (gen_random_uuid(), %s, 5500000, 'NGN', 'starter', 'monthly', now() at time zone 'utc', %s)",
                (f"ref_{RUN}", ORG_ID))
check(True, "payment inserted for that organisation")

print("\n--- Login, CSRF, lockout, replay ---")
NAME = f"ops{RUN}"
secret = make_admin(NAME)
tok = csrf_from(get(c, "/ops/login"))
r = post(c, "/ops/login", {"username": NAME, "password": "wrong"}, tok)
check(r.status_code == 200 and b"Invalid credentials" in r.data, "wrong password refused")
r = c.post("/ops/login", data={"username": NAME, "password": PW}, base_url=BASE)
check(r.status_code == 400, "a POST without the CSRF token is refused")
step = int(time.time() // 30)
r = full_login(c, NAME, PW, secret, step)
check(r.status_code == 302 and r.headers["Location"].endswith("/ops/"), "password + authenticator code signs in")
r = get(c, "/ops/")
check(r.status_code == 200 and b"Dashboard" in r.data, "dashboard renders")
c2 = app.test_client()
r = full_login(c2, NAME, PW, secret, step)
check(r.status_code == 200 and b"Invalid code" in r.data, "the same code can't be replayed")
c3 = app.test_client()
tok = csrf_from(get(c3, "/ops/login"))
post(c3, "/ops/login", {"username": NAME, "password": PW}, tok)
tok = csrf_from(get(c3, "/ops/2fa"))
r = post(c3, "/ops/2fa", {"code": "000000"}, tok)
check(r.status_code == 200 and b"Invalid code" in r.data, "a wrong authenticator code is refused")

LOCKED = f"lock{RUN}"
lsecret = make_admin(LOCKED)
cl = app.test_client()
tok = csrf_from(get(cl, "/ops/login"))
for _ in range(5):
    post(cl, "/ops/login", {"username": LOCKED, "password": "nope"}, tok)
r = post(cl, "/ops/login", {"username": LOCKED, "password": PW}, tok)
check(r.status_code == 200 and b"Too many" in r.data, "5 failures lock the account, even for the right password")

print("\n--- Read-only views ---")
r = get(c, f"/ops/subscribers?q={ORG.replace(' ', '+')}")
check(r.status_code == 200 and ORG.encode() in r.data, "subscriber search finds the organisation")
r = get(c, f"/ops/subscribers/{ORG_ID}")
check(r.status_code == 200 and f"u{RUN}".encode() in r.data and f"ref_{RUN}".encode() in r.data,
      "subscriber page shows users and payments")
r = get(c, "/ops/payments?currency=NGN")
check(r.status_code == 200 and f"ref_{RUN}".encode() in r.data and b"55,000" in r.data,
      "payments page shows the payment across tenants")
r = get(c, "/ops/audit")
check(r.status_code == 200 and b"view_subscriber" in r.data and b"login" in r.data, "audit log records activity")
print("\n--- Pricing console ---")


def db_val(sql, *args):
    with dbc("DATABASE_URL") as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def fresh_code():
    """A code that hasn't been used yet (resets the replay guard)."""
    with dbc("DATABASE_URL") as conn, conn.cursor() as cur:
        cur.execute("update ops_admins set last_totp_step = null, failed_attempts = 0, locked_until = null "
                    "where username = %s", (NAME,))
    return totp._code(secret, int(time.time() // 30))


def wat(dt):
    return (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")


PRICE_SQL = "select amount_minor from price_list where plan_key='starter' and interval='monthly' and currency='NGN'"
r = get(c, "/ops/pricing")
check(r.status_code == 200 and b"Prices and standing discounts" in r.data, "pricing page renders")
tok = csrf_from(r)
price = {"plan_key": "starter", "interval": "monthly", "currency": "NGN", "amount": "56000", "discount": "0"}

post(c, "/ops/pricing/price", {**price, "code": "000000"}, tok)
check(db_val(PRICE_SQL) == 5_500_000, "a wrong authenticator code changes nothing")
post(c, "/ops/pricing/price", {**price, "code": fresh_code()}, tok)
check(db_val(PRICE_SQL) == 5_600_000, "a valid code saves the price")
check(db_val("select count(*) from ops_audit_log where action = 'pricing_price'") >= 1, "the change is in the audit log")
post(c, "/ops/pricing/price", {**price, "amount": "90000", "code": fresh_code()}, tok)
check(db_val(PRICE_SQL) == 5_600_000, "a change of more than 30% is refused without the confirmation box")
post(c, "/ops/pricing/price", {**price, "amount": "90000", "confirm_large": "on", "code": fresh_code()}, tok)
check(db_val(PRICE_SQL) == 9_000_000, "...and accepted with it")
post(c, "/ops/pricing/price", {**price, "amount": "55000", "confirm_large": "on", "code": fresh_code()}, tok)
check(db_val(PRICE_SQL) == 5_500_000, "the price is restored")
post(c, "/ops/pricing/price", {**price, "amount": "abc", "code": fresh_code()}, tok)
post(c, "/ops/pricing/price", {**price, "discount": "95", "confirm_large": "on", "code": fresh_code()}, tok)
check(db_val(PRICE_SQL) == 5_500_000, "a non-numeric price and a 95% discount are refused")
check(post(c, "/ops/pricing/price", {**price, "currency": "EUR", "code": fresh_code()}, tok).status_code == 400,
      "an unknown currency is rejected")

ANNUAL = {"plan_key": "starter", "interval": "annual", "currency": "NGN", "amount": "660000"}
post(c, "/ops/pricing/price", {**ANNUAL, "discount": "20", "code": fresh_code()}, tok)
check(db_val("select discount_bp from price_list where plan_key='starter' and interval='annual' and currency='NGN'") == 2000
      and b"-20%" in app.test_client().get("/pricing").data, "a new standing discount reaches the customer pricing page")
post(c, "/ops/pricing/price", {**ANNUAL, "discount": "15", "code": fresh_code()}, tok)
check(db_val("select discount_bp from price_list where plan_key='starter' and interval='annual' and currency='NGN'") == 1500,
      "the annual discount is restored to 15%")

print("\n--- VAT and campaigns ---")
VAT_SQL = "select vat_bp::text || ':' || enabled::text from tax_settings where currency='NGN'"
post(c, "/ops/pricing/vat", {"currency": "NGN", "rate": "50", "enabled": "on", "code": fresh_code()}, tok)
check(db_val(VAT_SQL) == "750:false", "a VAT rate above 30% is refused")
post(c, "/ops/pricing/vat", {"currency": "NGN", "rate": "7.5", "enabled": "on", "code": fresh_code()}, tok)
check(db_val(VAT_SQL) == "750:true", "VAT can be switched on")
post(c, "/ops/pricing/vat", {"currency": "NGN", "rate": "7.5", "code": fresh_code()}, tok)
check(db_val(VAT_SQL) == "750:false", "and off again")

now = datetime.utcnow()
NAME_C = f"BF {RUN}"
form = {"name": NAME_C, "percent": "20", "plan_key": "", "interval": "annual", "currency": "",
        "starts": wat(now - timedelta(hours=1)), "ends": wat(now + timedelta(days=2))}
count_sql = "select count(*) from campaigns where name = %s"
post(c, "/ops/pricing/campaigns", {**form, "ends": wat(now - timedelta(hours=2)), "code": fresh_code()}, tok)
check(db_val(count_sql, NAME_C) == 0, "a campaign that ends before it starts is refused")
post(c, "/ops/pricing/campaigns", {**form, "percent": "60", "code": fresh_code()}, tok)
check(db_val(count_sql, NAME_C) == 0, "a discount above 50% needs the confirmation box")
post(c, "/ops/pricing/campaigns", {**form, "code": fresh_code()}, tok)
check(db_val(count_sql, NAME_C) == 1, "a valid campaign is created")
check(NAME_C.encode() in app.test_client().get("/pricing").data, "the customer pricing page shows the campaign")
cid = db_val("select id::text from campaigns where name = %s", NAME_C)
post(c, f"/ops/pricing/campaigns/{cid}/toggle", {"code": fresh_code()}, tok)
check(db_val("select active from campaigns where name = %s", NAME_C) is False
      and NAME_C.encode() not in app.test_client().get("/pricing").data, "switching it off removes it from the pricing page")
check(b"campaign_create" in get(c, "/ops/pricing").data, "campaign changes appear in the pricing history")
tok = csrf_from(get(c, "/ops/"))
check(post(c, "/ops/logout", {}, tok).status_code == 302, "logout works")
check(get(c, "/ops/").status_code == 302, "logged out means no access")

print("\n--- Privacy: what the console's database login can and cannot do ---")


def refused(sql):
    conn = dbc("OPS_DATABASE_URL")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        return False
    except psycopg2.errors.InsufficientPrivilege:
        return True
    finally:
        conn.close()


for sql in ("select * from journal_entries", "select * from journal_lines", "select * from accounts",
            "select * from employees", "select * from payslips", "select * from payroll_runs",
            "select * from bank_transactions", "select * from invoices", "select * from bills",
            "select * from customers", "select * from vendors", "select * from payment_settings",
            "select password_hash from users", "select * from users",
            "select paystack_email_token from organizations", "select * from organizations"):
            "select * from price_offers", "delete from price_list", "delete from campaigns",
            "update price_list set id = gen_random_uuid()",
    check(refused(sql), f"refused: {sql}")
for sql in ("update organizations set name = 'x'", "delete from subscription_payments",
            "delete from organizations", "update users set active = false",
            "insert into subscription_payments (id, reference, amount_minor, currency, paid_at, organization_id) "
            f"values (gen_random_uuid(), 'x{RUN}', 1, 'NGN', now(), '{ORG_ID}')"):
    check(refused(sql), f"read-only: {sql[:48]}")
conn = dbc("OPS_DATABASE_URL")
with conn.cursor() as cur:
    cur.execute("select reference from subscription_payments")
    check(f"ref_{RUN}" in [row[0] for row in cur.fetchall()], "it can read payments across tenants")
conn.close()

print("\nAll ops console checks passed.")