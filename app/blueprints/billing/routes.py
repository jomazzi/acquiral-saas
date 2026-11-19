import logging
import re
import uuid

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.tenant import User
from app.models.billing import SubscriptionPayment
from app.billing import plans, paystack, service, quotes
from app.billing.access import access_state, billing_enforced, trial_days_left, user_limit, utcnow

log = logging.getLogger("acquiral.billing")

billing_bp = Blueprint("billing", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Endpoints a tenant can still POST to when its subscription has lapsed
# (the app is otherwise read-only): paying, and the basics of being
# logged in. Everything else that mutates data is blocked.
WRITE_ALLOWED_ENDPOINTS = {
    "billing.checkout", "billing.callback", "billing.cancel", "billing.webhook",
    "auth.logout", "auth.change_password",
}


def enforce_billing():
    """Called from app-wide before_request (see app/__init__.py) for a
    logged-in user. Lapsed trial/subscription = read-only, never locked
    out: people can always still see and export their own books."""
    from app.billing.access import is_writable
    org = current_user.organization
    if org.is_demo or is_writable(org):
        return None
    if request.method == "GET" or request.endpoint in WRITE_ALLOWED_ENDPOINTS:
        return None
    flash("Your trial or subscription has ended, so Acquiral is read-only. "
          "Choose a plan to keep making changes.", "error")
    return redirect(url_for("billing.billing_page"))


def _plan_cards():
    """One dict per plan for the templates, priced from the database.
    `prices` is what the customer pays (ex-VAT), `listed` the list price,
    `was` the list price when a discount applies, `off` the discount percent,
    `bonus` the free months a new subscriber gets. All pre-formatted."""
    cards = []
    now = utcnow()
    running = quotes.running_bonuses(now)
    for key, p in plans.PLANS.items():
        prices, listed, was, off, saving, bonus = {}, {}, {}, {}, {}, {}
        for cur in quotes.sellable_currencies():
            prices[cur], listed[cur], was[cur], off[cur], bonus[cur] = {}, {}, {}, {}, {}
            qs = {i: quotes.quote(key, i, cur) for i in plans.INTERVALS}
            for i, q in qs.items():
                if q is None:
                    prices[cur][i], listed[cur][i], was[cur][i], off[cur][i], bonus[cur][i] = "—", "—", None, None, 0
                    continue
                prices[cur][i] = plans.format_money(q["net_minor"], cur, minor=True)
                listed[cur][i] = plans.format_money(q["list_minor"], cur, minor=True)
                was[cur][i] = listed[cur][i] if q["discount_minor"] else None
                off[cur][i] = f"{q['discount_bp'] / 100:g}" if q["discount_minor"] else None
                b = quotes.pick_bonus(running, key, i, cur, now)
                bonus[cur][i] = b["bonus_months"] if b else 0
            m, a = qs.get("monthly"), qs.get("annual")
            s = (m["list_minor"] * 12 - a["net_minor"]) if m and a else 0
            saving[cur] = plans.format_money(s, cur, minor=True) if s > 0 else None
        cards.append({"key": key, "name": p["name"], "tagline": p["tagline"],
                      "feature_list": p["feature_list"], "max_users": p["max_users"],
                      "prices": prices, "listed": listed, "was": was, "off": off, "saving": saving,
                      "bonus": bonus})
    return cards


def _quote_table(org):
    """Every plan x period x currency quote for this organisation, for the
    billing page summary (including any bonus months it is eligible for)."""
    table = {}
    now = utcnow()
    running = quotes.running_bonuses(now) if quotes.bonus_eligible(org) else []
    for key in plans.PLANS:
        for i in plans.INTERVALS:
            for cur in quotes.sellable_currencies():
                q = quotes.quote(key, i, cur)
                if q is None:
                    continue
                b = quotes.pick_bonus(running, key, i, cur, now)
                money = lambda v: plans.format_money(v, cur, minor=True)
                table[f"{key}|{i}|{cur}"] = {
                    "list": money(q["list_minor"]),
                    "standing": money(q["standing_minor"]), "standing_pct": q["standing_bp"] / 100,
                    "campaign": q["campaign_name"], "campaign_amt": money(q["campaign_minor"]),
                    "campaign_pct": q["campaign_bp"] / 100,
                    "net": money(q["net_minor"]), "vat": money(q["vat_minor"]), "vat_pct": q["vat_bp"] / 100,
                    "total": money(q["total_minor"]), "total_minor": q["total_minor"],
                    "bonus_months": b["bonus_months"] if b else 0, "bonus_name": b["name"] if b else None,
                    "period_months": 12 if i == "annual" else 1,
                }
    return table


@billing_bp.route("/pricing")
def pricing():
    return render_template("billing/pricing.html", cards=_plan_cards(), features=plans.FEATURES,
                           trial_days=plans.TRIAL_DAYS, currencies=quotes.sellable_currencies(),
                           campaign=quotes.banner(), bonus=quotes.bonus_banner())

@billing_bp.app_context_processor
def _promo_context():
    def promo():
        try:
            return {"campaign": quotes.banner(), "bonus": quotes.bonus_banner()}
        except Exception:
            db.session.rollback()
            return {"campaign": None, "bonus": None}
    return {"promo": promo}                      

@billing_bp.route("/billing")
@login_required
@admin_required
def billing_page():
    org = current_user.organization
    state = access_state(org)
    payments = (SubscriptionPayment.query.order_by(SubscriptionPayment.paid_at.desc()).limit(24).all())
    active_users = User.query.filter_by(organization_id=org.id, active=True).count()
    cur_list = quotes.sellable_currencies()
    default_cur = org.plan_currency if org.plan_currency in cur_list else cur_list[0]
    recurring_live = state in ("active", "past_due") and bool(org.paystack_subscription_code)
    prepaid = state == "active" and not org.paystack_subscription_code
    return render_template(
        "billing/billing.html", org=org, state=state, payments=payments,
        cards=_plan_cards(), features=plans.FEATURES, enforced=billing_enforced(),
        trial_days_left=trial_days_left(org), active_users=active_users,
        user_limit=user_limit(org), plans=plans, currencies=plans.CURRENCIES,
        default_currency=default_cur,
        can_cancel=bool(org.paystack_subscription_code and org.paystack_email_token
                        and state in ("active", "past_due")),
        # A live recurring subscription must be cancelled before starting
        # another, or the customer would be billed for both.
        has_live_subscription=recurring_live, prepaid=prepaid,
        quote_table=_quote_table(org), campaign=quotes.banner(),
    )


@billing_bp.route("/billing/checkout", methods=["POST"])
@login_required
@admin_required
def checkout():
    org = current_user.organization
    if org.is_demo:
        abort(403)
    plan_key = request.form.get("plan", "")
    interval = request.form.get("interval", "")
    currency = request.form.get("currency", plans.DEFAULT_CURRENCY)
    email = request.form.get("email", "").strip()
    mode = request.form.get("mode", "subscribe")
    if mode not in ("subscribe", "once"):
        abort(400)

    if plan_key not in plans.PLANS or interval not in plans.INTERVALS or currency not in plans.CURRENCIES:
        flash("Choose a plan, billing period and currency.", "error")
        return redirect(url_for("billing.billing_page"))
    if currency not in quotes.sellable_currencies():
        flash(f"Payments in {currency} aren't available yet. Please choose another currency.", "error")
        return redirect(url_for("billing.billing_page"))
    if not EMAIL_RE.match(email):
        flash("Enter a valid billing email — receipts and renewal notices go there.", "error")
        return redirect(url_for("billing.billing_page"))
    state = access_state(org)
    recurring_live = state in ("active", "past_due") and bool(org.paystack_subscription_code)
    prepaid_live = state == "active" and not org.paystack_subscription_code
    if mode == "subscribe" and (recurring_live or prepaid_live):
        if recurring_live:
            flash("You already have a live subscription. Cancel it first (it stays active until the "
                  "end of the period you paid for), then choose the new plan.", "error")
        else:
            until = org.current_period_end.strftime("%d %b %Y") if org.current_period_end else "the end of this period"
            flash(f"You're paid up until {until}. Pay for the next period, or subscribe once this one has ended.", "error")
        return redirect(url_for("billing.billing_page"))
    if mode == "once" and recurring_live:
        flash("You have a recurring subscription. Cancel it first (it stays active until the end of the "
              "period you paid for), then you can pay one period at a time.", "error")
        return redirect(url_for("billing.billing_page"))

    active_users = User.query.filter_by(organization_id=org.id, active=True).count()
    if active_users > plans.max_users_for(plan_key):
        flash(f"{plans.PLANS[plan_key]['name']} allows up to {plans.max_users_for(plan_key)} users and you have "
              f"{active_users} active. Deactivate some users or choose a larger plan.", "error")
        return redirect(url_for("billing.billing_page"))

    q = quotes.quote(plan_key, interval, currency)
    if q is None or not paystack.is_configured():
        log.error("Checkout attempted but %s/%s/%s isn't for sale or Paystack isn't configured",
                  plan_key, interval, currency)
        flash("Online payment isn't available for that plan yet. Please contact Admiral Sentinel to subscribe.", "error")
        return redirect(url_for("billing.billing_page"))
    expected = request.form.get("expected_total", "").strip()
    if expected and expected != str(q["total_minor"]):
        flash("The price changed while you were on this page. Please review the new total and confirm again.", "error")
        return redirect(url_for("billing.billing_page"))

    reference = f"acq_{org.id.hex[:12]}_{uuid.uuid4().hex[:16]}"
    callback_url = url_for("billing.callback", _external=True)
    metadata = {"organization_id": str(org.id), "plan_key": plan_key, "interval": interval, "currency": currency}
    try:
        if mode == "once":
            metadata.update({"mode": "once", "list_minor": q["list_minor"], "discount_minor": q["discount_minor"],
                             "vat_bp": q["vat_bp"], "vat_minor": q["vat_minor"]})
            init = paystack.initialize_transaction(
                email=email, amount_minor=q["total_minor"], currency=currency, plan_code=None,
                reference=reference, callback_url=callback_url, metadata=metadata)
        else:
            offer = quotes.get_or_create_offer(q)
            metadata["offer_id"] = str(offer.id)
            init = paystack.initialize_transaction(
                email=email, amount_minor=q["total_minor"], currency=currency, plan_code=offer.paystack_plan_code,
                reference=reference, callback_url=callback_url, metadata=metadata)
    except paystack.PaystackError as e:
        log.error("Could not start the %s payment for org %s: %s", mode, org.id, e)
        flash("We couldn't prepare the payment just now. Please try again in a moment.", "error")
        return redirect(url_for("billing.billing_page"))


    org.billing_email = email
    db.session.commit()
    return redirect(init["authorization_url"])


@billing_bp.route("/billing/callback")
@login_required
def callback():
    """Where Paystack sends the customer after checkout. Never trusts the
    query string: re-verifies the transaction with Paystack and checks it
    belongs to the logged-in organisation before activating anything."""
    reference = request.args.get("reference") or request.args.get("trxref") or ""
    org = current_user.organization
    if not reference:
        return redirect(url_for("billing.billing_page"))
    try:
        data = paystack.verify_transaction(reference)
    except paystack.PaystackError as e:
        log.error("Paystack verify failed for %s: %s", reference, e)
        flash("We couldn't confirm your payment yet. If you were charged, your plan will activate "
              "automatically within a few minutes.", "error")
        return redirect(url_for("billing.billing_page"))

    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if str(meta.get("organization_id")) != str(org.id):
        abort(403)
    if data.get("status") != "success":
        if data.get("status") in ("pending", "ongoing", "processing", "queued"):
            flash("We're waiting for your bank to confirm the payment. Your plan will activate "
                  "automatically as soon as it does.", "success")
        else:
            flash("The payment wasn't completed, so you haven't been charged. You can try again.", "error")
        return redirect(url_for("billing.billing_page"))

    service.apply_successful_charge(org, data)
    if meta.get("mode") == "once":
        db.session.refresh(org)
        until = org.current_period_end.strftime("%d %b %Y") if org.current_period_end else "the end of the period"
        flash(f"Payment received — thank you! You're paid up until {until}.", "success")
    else:
        flash("Payment received — thank you! Your subscription is active.", "success")
    return redirect(url_for("billing.billing_page"))


@billing_bp.route("/billing/cancel", methods=["POST"])
@login_required
@admin_required
def cancel():
    org = current_user.organization
    if not (org.paystack_subscription_code and org.paystack_email_token):
        flash("We don't have a recurring subscription on file to cancel.", "error")
        return redirect(url_for("billing.billing_page"))
    try:
        paystack.disable_subscription(org.paystack_subscription_code, org.paystack_email_token)
    except paystack.PaystackError as e:
        log.error("Paystack disable failed for org %s: %s", org.id, e)
        flash("We couldn't cancel just now. Please try again, or contact us.", "error")
        return redirect(url_for("billing.billing_page"))
    org.billing_status = "canceled"
    db.session.commit()
    flash("Subscription cancelled. You keep full access until the end of the period you've paid for.", "success")
    return redirect(url_for("billing.billing_page"))


@billing_bp.route("/billing/webhook/paystack", methods=["POST"])
def webhook():
    """Paystack -> us. Authenticated solely by the HMAC signature over the
    raw body; there is no session. Answers 200 to every validly-signed
    event (even ones we ignore) so Paystack doesn't retry them, and 5xx
    only if processing genuinely failed, so Paystack DOES retry that."""
    raw = request.get_data()
    if not paystack.valid_webhook_signature(raw, request.headers.get("X-Paystack-Signature", "")):
        return "Invalid signature", 401

    payload = request.get_json(silent=True) or {}
    event, data = payload.get("event", ""), payload.get("data") or {}
    try:
        outcome = service.handle_event(event, data)
    except Exception:
        db.session.rollback()
        log.exception("Paystack webhook processing failed for event %s", event)
        return "Processing error", 500
    log.info("Paystack webhook %s -> %s", event, outcome)
    return jsonify({"received": True})
