"""Pay Now: the public invoice page, card checkout on the organisation's
OWN Paystack account, "I've paid" transfer claims, and admin settings.

Public routes (`/i/<token>`, `/pay/webhook/<org_id>`) have no login. They
find their tenant through the non-RLS invoice_share_links table (or the
org id in the webhook URL), then call set_tenant() before touching any
tenant data. Pages are standalone (no base.html) so no staff UI leaks.
"""
import logging
import re
import uuid

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user

from app import set_tenant
from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import Account
from app.models.invoicing import Invoice
from app.models.payments import InvoicePayment, PaymentSettings
from app.models.tenant import Organization
from app.billing import paystack
from app.payments import crypto, service

log = logging.getLogger("acquiral.pay")
pay_bp = Blueprint("pay", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_OPEN_CLAIMS = 5


def _public_invoice(token):
    invoice, settings = service.resolve_token(token)
    if not invoice:
        abort(404)
    return invoice, settings


@pay_bp.route("/i/<token>")
def public_invoice(token):
    invoice, settings = _public_invoice(token)
    org = db.session.get(Organization, invoice.organization_id)
    claimed = InvoicePayment.query.filter_by(invoice_id=invoice.id, status="claimed").count() > 0
    return render_template(
        "pay/invoice_public.html", invoice=invoice, org=org, settings=settings, token=token,
        card_enabled=bool(settings and settings.card_enabled and service.org_paystack_key(settings)
                          and invoice.status == "sent"),
        show_bank=bool(settings and settings.show_bank_details and settings.bank_account),
        claimed=claimed, result=request.args.get("r"),
    ), 200, {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


@pay_bp.route("/i/<token>/card", methods=["POST"])
def public_card(token):
    invoice, settings = _public_invoice(token)
    back = redirect(url_for("pay.public_invoice", token=token))
    key = service.org_paystack_key(settings)
    if invoice.status != "sent" or not (settings and settings.card_enabled and key):
        return back
    email = request.form.get("email", "").strip()
    if not EMAIL_RE.match(email):
        return redirect(url_for("pay.public_invoice", token=token, r="bademail"))
    reference = f"inv_{invoice.id.hex[:12]}_{uuid.uuid4().hex[:16]}"
    try:
        init = paystack.initialize_transaction(
            email=email, amount_minor=service.amount_minor(invoice), currency="NGN", plan_code=None,
            reference=reference, callback_url=url_for("pay.public_return", token=token, _external=True),
            metadata={"invoice_id": str(invoice.id), "organization_id": str(invoice.organization_id),
                      "invoice_number": invoice.invoice_number}, key=key)
    except paystack.PaystackError as e:
        log.error("Pay Now init failed for invoice %s: %s", invoice.id, e)
        return redirect(url_for("pay.public_invoice", token=token, r="error"))
    return redirect(init["authorization_url"])


@pay_bp.route("/i/<token>/return")
def public_return(token):
    invoice, settings = _public_invoice(token)
    reference = request.args.get("reference") or request.args.get("trxref") or ""
    key = service.org_paystack_key(settings)
    if not reference or not key:
        return redirect(url_for("pay.public_invoice", token=token))
    try:
        data = paystack.verify_transaction(reference, key=key)
    except paystack.PaystackError as e:
        log.error("Pay Now verify failed for %s: %s", reference, e)
        return redirect(url_for("pay.public_invoice", token=token, r="pending"))
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if str(meta.get("invoice_id")) != str(invoice.id):
        abort(403)
    if data.get("status") != "success":
        return redirect(url_for("pay.public_invoice", token=token, r="failed"))
    outcome = service.settle_card_payment(invoice.id, invoice.organization_id, data)
    return redirect(url_for("pay.public_invoice", token=token,
                            r="paid" if outcome in ("paid", "already") else "review"))


@pay_bp.route("/i/<token>/claim", methods=["POST"])
def public_claim(token):
    invoice, settings = _public_invoice(token)
    if invoice.status != "sent":
        return redirect(url_for("pay.public_invoice", token=token))
    open_claims = InvoicePayment.query.filter_by(invoice_id=invoice.id, status="claimed").count()
    if open_claims >= MAX_OPEN_CLAIMS:
        return redirect(url_for("pay.public_invoice", token=token, r="claimed"))
    name = request.form.get("name", "").strip()[:150]
    note = request.form.get("note", "").strip()[:500]
    if not name:
        return redirect(url_for("pay.public_invoice", token=token, r="noname"))
    db.session.add(InvoicePayment(
        organization_id=invoice.organization_id, invoice_id=invoice.id,
        reference=f"claim_{uuid.uuid4().hex}", method="transfer", status="claimed",
        amount_minor=service.amount_minor(invoice), payer_name=name, note=note))
    db.session.commit()
    return redirect(url_for("pay.public_invoice", token=token, r="claimed"))


@pay_bp.route("/pay/webhook/<uuid:org_id>", methods=["POST"])
def org_webhook(org_id):
    """Paystack (the organisation's account) -> us. Authenticated by the
    HMAC signature made with THAT organisation's secret key, so a forged
    request for another tenant fails. Always 200 for signed events."""
    set_tenant(org_id)
    settings = service.get_settings(org_id)
    key = service.org_paystack_key(settings)
    raw = request.get_data()
    if not key or not paystack.valid_webhook_signature(raw, request.headers.get("X-Paystack-Signature", ""), key=key):
        return "Invalid signature", 401
    payload = request.get_json(silent=True) or {}
    if payload.get("event") != "charge.success":
        return jsonify({"received": True})
    data = payload.get("data") or {}
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    try:
        invoice_id = uuid.UUID(str(meta.get("invoice_id")))
    except ValueError:
        return jsonify({"received": True})   # a charge unrelated to an invoice
    try:
        outcome = service.settle_card_payment(invoice_id, org_id, data)
    except Exception:
        db.session.rollback()
        log.exception("Pay Now webhook failed for org %s", org_id)
        return "Processing error", 500
    log.info("Pay Now webhook invoice %s -> %s", invoice_id, outcome)
    return jsonify({"received": True})


# ---------------------------------------------------------------- admin

@pay_bp.route("/settings/payments", methods=["GET", "POST"])
@login_required
@admin_required
def settings_page():
    org = current_user.organization
    settings = service.get_settings(org.id)
    if request.method == "POST":
        if org.is_demo:
            abort(403)
        if not settings:
            settings = PaymentSettings(organization_id=org.id)
            db.session.add(settings)
        action = request.form.get("action", "save")
        if action == "remove_key":
            settings.paystack_secret_enc = None
            settings.paystack_key_hint = None
            db.session.commit()
            flash("Paystack key removed. Card payments are off.", "success")
            return redirect(url_for("pay.settings_page"))

        bank_id = request.form.get("bank_account_id") or None
        bank = Account.query.filter_by(id=bank_id, is_cash_or_bank=True).first() if bank_id else None
        if bank_id and (not bank or bank.currency != "NGN"):
            flash("Choose a Naira bank account.", "error")
            return redirect(url_for("pay.settings_page"))
        settings.bank_account_id = bank.id if bank else None
        settings.show_bank_details = bool(request.form.get("show_bank_details"))
        settings.bank_instructions = request.form.get("bank_instructions", "").strip()[:1000] or None

        new_key = request.form.get("paystack_key", "").strip()
        if new_key:
            if not re.fullmatch(r"sk_(live|test)_[A-Za-z0-9]{10,}", new_key):
                db.session.rollback()
                flash("That doesn't look like a Paystack secret key (it starts with sk_live_ or sk_test_).", "error")
                return redirect(url_for("pay.settings_page"))
            try:
                paystack.check_key(new_key)
            except paystack.PaystackError:
                db.session.rollback()
                flash("Paystack rejected that key. Copy the secret key again from Paystack → Settings → API Keys.", "error")
                return redirect(url_for("pay.settings_page"))
            settings.paystack_secret_enc = crypto.encrypt(new_key)
            settings.paystack_key_hint = f"{new_key[:8]}…{new_key[-4:]}"
        db.session.commit()
        flash("Payment settings saved.", "success")
        return redirect(url_for("pay.settings_page"))

    banks = Account.query.filter_by(is_cash_or_bank=True, active=True, currency="NGN").order_by(Account.code).all()
    return render_template("pay/settings.html", settings=settings, banks=banks,
                           webhook_url=url_for("pay.org_webhook", org_id=org.id, _external=True),
                           key_ok=bool(settings and service.org_paystack_key(settings)))


@pay_bp.route("/invoices/<uuid:invoice_id>/claims/<uuid:payment_id>/<action>", methods=["POST"])
@login_required
@admin_required
def claim_action(invoice_id, payment_id, action):
    if action not in ("confirm", "dismiss"):
        abort(404)
    pay = InvoicePayment.query.filter_by(id=payment_id, invoice_id=invoice_id, status="claimed").first_or_404()
    if action == "dismiss":
        pay.status = "dismissed"
        db.session.commit()
        flash("Claim dismissed.", "success")
    else:
        settings = service.get_settings(current_user.organization_id)
        bank = Account.query.filter_by(id=request.form.get("bank_account_id"), is_cash_or_bank=True).first() \
            or (settings.bank_account if settings else None)
        if not bank or bank.currency != "NGN":
            flash("Choose the Naira account that received the transfer.", "error")
        elif service.confirm_claim(pay, current_user.id, bank):
            flash("Transfer confirmed — invoice marked as paid.", "success")
        else:
            flash("That invoice can no longer be marked paid from this claim.", "error")
    return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))
