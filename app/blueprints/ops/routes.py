"""Ops console: subscription administration for the operator of Acquiral.
Served ONLY on OPS_HOST (404 everywhere else). Own login (password + 2FA),
own session, and a restricted database role that cannot read customers'
accounting, payroll or bank data. Release 1 is read-only."""
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace

from flask import Blueprint, abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app import mailer
from app.billing import paystack, plans
from app.billing.access import access_state, utcnow
from app.blueprints.ops import store, totp
from app.blueprints.ops.security import dec

ops_bp = Blueprint("ops", __name__, url_prefix="/ops")

IDLE = timedelta(minutes=30)
ABSOLUTE = timedelta(hours=12)
PENDING = timedelta(minutes=5)
MAX_FAILS = 5
LOCK_FOR = timedelta(minutes=15)
PAGE = 50
STATUSES = ("trialing", "active", "past_due", "canceled", "comped")
CURRENCIES = ("NGN", "USD", "EUR", "GBP")
_DUMMY_HASH = generate_password_hash("not-a-real-password")


@ops_bp.before_request
def _gate():
    host = request.host.split(":")[0].lower()
    if not store.enabled() or host != os.environ["OPS_HOST"].strip().lower():
        abort(404)
    session.setdefault("ops_csrf", secrets.token_urlsafe(24))
    if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), session["ops_csrf"]):
        abort(400)


@ops_bp.after_request
def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@ops_bp.context_processor
def _ctx():
    return {"csrf": session.setdefault("ops_csrf", secrets.token_urlsafe(24))}


@ops_bp.app_template_filter("opsmoney")
def _money(minor, currency):
    return plans.format_money(int(minor or 0), currency, minor=True)


@ops_bp.app_template_filter("opsdt")
def _dt(value):
    return value.strftime("%d %b %Y %H:%M") if value else "—"


def _audit(action, org_id=None, detail="", username=None):
    store.execute(
        "insert into ops_audit_log (admin_username, action, organization_id, detail, ip) "
        "values (%s, %s, %s::uuid, %s, %s)",
        (username or session.get("ops_user"), action, org_id, (detail or "")[:500], request.remote_addr))


def _register_failure(admin):
    fails = admin["failed_attempts"] + 1
    if fails >= MAX_FAILS:
        store.execute("update ops_admins set failed_attempts=0, locked_until=%s where id=%s::uuid",
                      (utcnow() + LOCK_FOR, admin["id"]))
    else:
        store.execute("update ops_admins set failed_attempts=%s where id=%s::uuid", (fails, admin["id"]))


def _current_admin():
    pid = session.get("ops_id")
    if not pid:
        return None
    now = utcnow()
    try:
        seen = datetime.fromisoformat(session["ops_seen"])
        began = datetime.fromisoformat(session["ops_login_at"])
    except (KeyError, ValueError):
        session.clear()
        return None
    if now - seen > IDLE or now - began > ABSOLUTE:
        session.clear()
        return None
    admin = store.one("select id::text as id, username, active from ops_admins where id=%s::uuid", (pid,))
    if not admin or not admin["active"]:
        session.clear()
        return None
    session["ops_seen"] = now.isoformat()
    return admin


def ops_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        admin = _current_admin()
        if admin is None:
            return redirect(url_for("ops.login"))
        g.ops = admin
        return view(*args, **kwargs)
    return wrapped


def _page():
    try:
        return max(1, int(request.args.get("page", "1")))
    except ValueError:
        return 1


@ops_bp.route("/login", methods=["GET", "POST"])
def login():
    if _current_admin():
        return redirect(url_for("ops.dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        admin = store.one(
            "select id::text as id, username, password_hash, active, failed_attempts, locked_until "
            "from ops_admins where lower(username)=%s", (username,))
        now = utcnow()
        if admin and admin["active"] and admin["locked_until"] and admin["locked_until"] > now:
            _audit("login_blocked", username=username)
            error = "Too many attempts. Try again later."
        elif admin and admin["active"] and check_password_hash(admin["password_hash"], password):
            session["ops_pending"] = admin["id"]
            session["ops_pending_at"] = now.isoformat()
            return redirect(url_for("ops.two_factor"))
        else:
            if admin is None:
                check_password_hash(_DUMMY_HASH, password)
            else:
                _register_failure(admin)
            _audit("login_failed", username=username)
            error = "Invalid credentials."
    return render_template("ops/login.html", error=error)


@ops_bp.route("/2fa", methods=["GET", "POST"])
def two_factor():
    pid = session.get("ops_pending")
    try:
        started = datetime.fromisoformat(session.get("ops_pending_at", ""))
    except ValueError:
        started = None
    if not pid or started is None or utcnow() - started > PENDING:
        session.pop("ops_pending", None)
        return redirect(url_for("ops.login"))
    error = None
    if request.method == "POST":
        admin = store.one(
            "select id::text as id, username, active, totp_secret_enc, last_totp_step, failed_attempts, "
            "locked_until from ops_admins where id=%s::uuid", (pid,))
        now = utcnow()
        if not admin or not admin["active"] or (admin["locked_until"] and admin["locked_until"] > now):
            session.pop("ops_pending", None)
            return redirect(url_for("ops.login"))
        step = totp.verify(dec(admin["totp_secret_enc"]), request.form.get("code", ""), admin["last_totp_step"])
        if step is None:
            _register_failure(admin)
            _audit("2fa_failed", username=admin["username"])
            error = "Invalid code."
        else:
            store.execute("update ops_admins set failed_attempts=0, locked_until=null, last_totp_step=%s, "
                          "last_login_at=%s where id=%s::uuid", (step, now, admin["id"]))
            session.clear()
            session["ops_csrf"] = secrets.token_urlsafe(24)
            session["ops_id"] = admin["id"]
            session["ops_user"] = admin["username"]
            session["ops_login_at"] = now.isoformat()
            session["ops_seen"] = now.isoformat()
            _audit("login", username=admin["username"])
            return redirect(url_for("ops.dashboard"))
    return render_template("ops/two_factor.html", error=error)


@ops_bp.route("/logout", methods=["POST"])
@ops_required
def logout():
    _audit("logout")
    session.clear()
    return redirect(url_for("ops.login"))


@ops_bp.route("/")
@ops_required
def dashboard():
    utc_now = "(now() at time zone 'utc')"
    totals = store.one(
        "select count(*) filter (where not is_demo) as orgs, "
        "count(*) filter (where not is_demo and billing_status='trialing') as trialing, "
        "count(*) filter (where not is_demo and billing_status='active') as active, "
        "count(*) filter (where not is_demo and billing_status='past_due') as past_due, "
        "count(*) filter (where not is_demo and billing_status='canceled') as canceled, "
        "count(*) filter (where not is_demo and billing_status='comped') as comped from organizations")
    users_n = store.one("select count(*) as n from users")["n"]
    month = store.rows(
        "select currency, count(*) as n, sum(amount_minor) as total from subscription_payments "
        f"where paid_at >= date_trunc('month', {utc_now}) group by currency order by currency")
    alltime = store.rows(
        "select currency, count(*) as n, sum(amount_minor) as total from subscription_payments "
        "group by currency order by currency")
    ending = store.rows(
        "select id::text as id, name, trial_ends_at from organizations "
        f"where billing_status='trialing' and not is_demo and trial_ends_at between {utc_now} "
        f"and {utc_now} + interval '7 days' order by trial_ends_at")
    attention = store.rows(
        "select id::text as id, name, billing_status, trial_ends_at, current_period_end from organizations "
        "where not is_demo and (billing_status='past_due' "
        f"or (billing_status in ('active','canceled') and current_period_end < {utc_now}) "
        f"or (billing_status='trialing' and trial_ends_at < {utc_now})) order by name")
    recent = store.rows(
        "select p.reference, p.amount_minor, p.currency, p.plan_key, p.plan_interval, p.paid_at, "
        "o.id::text as org_id, o.name from subscription_payments p join organizations o "
        "on o.id = p.organization_id order by p.paid_at desc limit 10")
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    system = {
        "head": store.one("select version_num from alembic_version")["version_num"],
        "paystack": ("LIVE" if key.startswith("sk_live") else "TEST" if key.startswith("sk_test")
                     else "not configured"),
        "mail": "configured" if mailer.mail_enabled() else "not configured",
        "now": utcnow(),
    }
    return render_template("ops/dashboard.html", t=totals, users_n=users_n, month=month, alltime=alltime,
                           ending=ending, attention=attention, recent=recent, system=system)


@ops_bp.route("/subscribers")
@ops_required
def subscribers():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    page = _page()
    where, params = ["true"], []
    if q:
        where.append("(o.name ilike %s or o.slug ilike %s or o.billing_email ilike %s)")
        params += [f"%{q}%"] * 3
    if status in STATUSES:
        where.append("o.billing_status = %s")
        params.append(status)
    orgs = store.rows(
        "select o.id::text as id, o.name, o.is_demo, o.billing_status, o.plan_key, o.plan_interval, "
        "o.plan_currency, o.trial_ends_at, o.current_period_end, o.created_at, "
        "(select count(*) from users u where u.organization_id = o.id and u.active) as users "
        f"from organizations o where {' and '.join(where)} order by o.created_at desc limit %s offset %s",
        params + [PAGE + 1, (page - 1) * PAGE])
    more = len(orgs) > PAGE
    orgs = orgs[:PAGE]
    for o in orgs:
        o["state"] = access_state(SimpleNamespace(**o))
    return render_template("ops/subscribers.html", orgs=orgs, q=q, status=status, statuses=STATUSES,
                           page=page, more=more)


@ops_bp.route("/subscribers/<uuid:org_id>")
@ops_required
def subscriber(org_id):
    oid = str(org_id)
    org = store.one(
        "select id::text as id, name, slug, created_at, active, is_demo, billing_status, billing_email, "
        "plan_key, plan_interval, plan_currency, trial_ends_at, current_period_end, "
        "paystack_customer_code, paystack_subscription_code from organizations where id = %s::uuid", (oid,))
    if not org:
        abort(404)
    users = store.rows(
        "select username, full_name, role, active, email, email_verified, terms_accepted_at, terms_version "
        "from users where organization_id = %s::uuid order by role desc, username", (oid,))
    payments = store.rows(
        "select reference, amount_minor, currency, plan_key, plan_interval, paid_at from subscription_payments "
        "where organization_id = %s::uuid order by paid_at desc limit 100", (oid,))
    org["state"] = access_state(SimpleNamespace(**org))
    _audit("view_subscriber", oid, org["name"])
    return render_template("ops/subscriber.html", o=org, users=users, payments=payments)


@ops_bp.route("/payments")
@ops_required
def payments():
    currency = request.args.get("currency", "")
    month = request.args.get("month", "")
    page = _page()
    where, params = ["true"], []
    if currency in CURRENCIES:
        where.append("p.currency = %s")
        params.append(currency)
    if re.fullmatch(r"\d{4}-\d{2}", month):
        where.append("to_char(p.paid_at, 'YYYY-MM') = %s")
        params.append(month)
    cond = " and ".join(where)
    totals = store.rows(
        f"select p.currency, count(*) as n, sum(p.amount_minor) as total from subscription_payments p "
        f"where {cond} group by p.currency order by p.currency", params)
    items = store.rows(
        "select p.reference, p.amount_minor, p.currency, p.plan_key, p.plan_interval, p.paid_at, "
        "o.id::text as org_id, o.name from subscription_payments p join organizations o "
        f"on o.id = p.organization_id where {cond} order by p.paid_at desc limit %s offset %s",
        params + [PAGE + 1, (page - 1) * PAGE])
    more = len(items) > PAGE
    return render_template("ops/payments.html", totals=totals, items=items[:PAGE], currency=currency,
                           month=month, currencies=CURRENCIES, page=page, more=more)


@ops_bp.route("/audit")
@ops_required
def audit():
    entries = store.rows(
        "select a.created_at, a.admin_username, a.action, a.organization_id::text as org_id, a.detail, a.ip "
        "from ops_audit_log a order by a.id desc limit 200")
    return render_template("ops/audit.html", entries=entries)