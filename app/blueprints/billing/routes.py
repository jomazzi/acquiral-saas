import logging
import re
import uuid

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.tenant import User
from app.models.billing import SubscriptionPayment
from app.billing import plans, paystack, service
from app.billing.access import access_state, billing_enforced, trial_days_left, user_limit

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
    """One dict per plan for the templates. `prices[currency][interval]`
    and `saving[currency]` are pre-formatted strings (e.g. '\u20a645,000'),
    so the templates never do money arithmetic."""
    cards = []
    for key, p in plans.PLANS.items():
        prices, saving = {}, {}
        for cur in plans.CURRENCIES:
            prices[cur] = {i: plans.format_money(plans.price(key, i, cur), cur) for i in plans.INTERVALS}
            s = plans.annual_saving(key, cur)
            saving[cur] = plans.format_money(s, cur) if s > 0 else None
        cards.append({"key": key, "name": p["name"], "tagline": p["tagline"],
                      "max_users": p["max_users"], "prices": prices, "saving": saving})
    return cards


@billing_bp.route("/pricing")
def pricing():
    return render_template("billing/pricing.html", cards=_plan_cards(), features=plans.FEATURES,
                           trial_days=plans.TRIAL_DAYS, currencies=plans.CURRENCIES)


@billing_bp.route("/billing")
@login_required
@admin_required
def billing_page():
    org = current_user.organization
    state = access_state(org)
    payments = (SubscriptionPayment.query.order_by(SubscriptionPayment.paid_at.desc()).limit(24).all())
    active_users = User.query.filter_by(organization_id=org.id, active=True).count()
    return render_template(
        "billing/billing.html", org=org, state=state, payments=payments,
        cards=_plan_cards(), features=plans.FEATURES, enforced=billing_enforced(),
        trial_days_left=trial_days_left(org), active_users=active_users,
        user_limit=user_limit(org), plans=plans, currencies=plans.CURRENCIES,
        default_currency=org.plan_currency or plans.DEFAULT_CURRENCY,
        can_cancel=bool(org.paystack_subscription_code and org.paystack_email_token
                        and state in ("active", "past_due")),
        # A live recurring subscription must be cancelled before starting
        # another, or the customer would be billed for both.
        has_live_subscription=state in ("active", "past_due"),
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

    if plan_key not in plans.PLANS or interval not in plans.INTERVALS or currency not in plans.CURRENCIES:
        flash("Choose a plan, billing period and currency.", "error")
        return redirect(url_for("billing.billing_page"))
    if not EMAIL_RE.match(email):
        flash("Enter a valid billing email — receipts and renewal notices go there.", "error")
        return redirect(url_for("billing.billing_page"))
    if access_state(org) in ("active", "past_due"):
        flash("You already have a live subscription. Cancel it first (it stays active until the "
              "end of the period you paid for), then choose the new plan.", "error")
        return redirect(url_for("billing.billing_page"))

    active_users = User.query.filter_by(organization_id=org.id, active=True).count()
    if active_users > plans.max_users_for(plan_key):
        flash(f"{plans.PLANS[plan_key]['name']} allows up to {plans.max_users_for(plan_key)} users and you have "
              f"{active_users} active. Deactivate some users or choose a larger plan.", "error")
        return redirect(url_for("billing.billing_page"))

    plan_code = plans.paystack_plan_code(plan_key, interval, currency)
    if not paystack.is_configured() or not plan_code:
        log.error("Checkout attempted but Paystack/plan code not configured (%s)",
                  plans.plan_code_env_var(plan_key, interval, currency))
        flash("Online payment isn't available yet. Please contact Admiral Sentinel to subscribe.", "error")
        return redirect(url_for("billing.billing_page"))

    reference = f"acq_{org.id.hex[:12]}_{uuid.uuid4().hex[:16]}"
    try:
        init = paystack.initialize_transaction(
            email=email, amount_minor=plans.price_minor(plan_key, interval, currency), currency=currency,
            plan_code=plan_code, reference=reference, callback_url=url_for("billing.callback", _external=True),
            metadata={"organization_id": str(org.id), "plan_key": plan_key, "interval": interval,
                      "currency": currency},
        )
    except paystack.PaystackError as e:
        log.error("Paystack initialize failed for org %s: %s", org.id, e)
        flash("We couldn't start the payment just now. Please try again in a moment.", "error")
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
        flash("The payment wasn't completed, so you haven't been charged. You can try again.", "error")
        return redirect(url_for("billing.billing_page"))

    service.apply_successful_charge(org, data)
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
