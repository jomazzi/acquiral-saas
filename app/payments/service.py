"""Pay Now: share links and settlement of invoice payments.

Money flow: the customer pays the ORGANISATION's own Paystack account
directly (we never hold funds). We only (a) create the checkout using the
org's stored key, (b) verify the transaction with that same key, and (c)
post the matching entry to the org's books.

Settlement is idempotent and race-safe: the invoice row is locked
(SELECT ... FOR UPDATE), InvoicePayment.reference is unique, and a payment
that arrives when the invoice is already paid, or with the wrong amount,
is recorded as 'duplicate'/'mismatch' and NOT posted (it needs a human).
"""
import logging
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError

from app import set_tenant
from app.extensions import db
from app.models.accounting import Account, JournalEntry, JournalLine
from app.models.invoicing import Invoice
from app.models.payments import InvoiceShareLink, InvoicePayment, PaymentSettings
from app.payments import crypto

log = logging.getLogger("acquiral.pay")


def get_settings(org_id):
    return PaymentSettings.query.filter_by(organization_id=org_id).first()


def org_paystack_key(settings):
    """Decrypted key, or None if absent/undecryptable (e.g. rotated SECRET_KEY)."""
    if not settings or not settings.paystack_secret_enc:
        return None
    try:
        return crypto.decrypt(settings.paystack_secret_enc)
    except crypto.DecryptError:
        log.error("Could not decrypt Paystack key for org %s", settings.organization_id)
        return None


def ensure_share_link(invoice):
    link = InvoiceShareLink.query.filter_by(invoice_id=invoice.id).first()
    if not link:
        link = InvoiceShareLink(organization_id=invoice.organization_id, invoice_id=invoice.id)
        db.session.add(link)
        db.session.commit()
        set_tenant(invoice.organization_id)
    return link


def resolve_token(token):
    """Public entry: token -> (invoice, settings) with tenant context set,
    or (None, None). Never reveals whether a token 'almost' matched."""
    if not token or len(token) > 64:
        return None, None
    link = db.session.get(InvoiceShareLink, token)
    if not link:
        return None, None
    set_tenant(link.organization_id)
    invoice = Invoice.query.filter_by(id=link.invoice_id).first()
    if not invoice or invoice.status == "draft":
        return None, None
    return invoice, get_settings(link.organization_id)


def amount_minor(invoice):
    return int(round(invoice.total * 100))


def settle_card_payment(invoice_id, org_id, data, key_label="paystack"):
    """Apply a VERIFIED successful Paystack transaction (`data` from
    /transaction/verify with the org's key) to the invoice.
    Returns 'paid' | 'already' | 'duplicate' | 'mismatch' | 'ignored'."""
    set_tenant(org_id)
    reference = data.get("reference")
    if not reference or data.get("status") != "success":
        return "ignored"
    if InvoicePayment.query.filter_by(reference=reference).first():
        return "already"

    invoice = (Invoice.query.filter_by(id=invoice_id).with_for_update().first())
    if not invoice:
        return "ignored"
    settings = get_settings(org_id)
    paid_minor = int(data.get("amount") or 0)
    cust = data.get("customer") or {}
    base = dict(organization_id=org_id, invoice_id=invoice.id, reference=reference,
                method="card", amount_minor=paid_minor, payer_email=cust.get("email"),
                paid_at=datetime.utcnow())

    if invoice.status == "paid":
        status = "duplicate"
    elif invoice.status != "sent" or data.get("currency") != "NGN" or paid_minor != amount_minor(invoice) \
            or not (settings and settings.bank_account_id):
        status = "mismatch"
    else:
        status = "paid"

    try:
        pay = InvoicePayment(status=status, **base)
        db.session.add(pay)
        if status == "paid":
            ar = Account.query.filter_by(code="2300").first()
            entry = JournalEntry(
                organization_id=org_id, entry_date=date.today(),
                memo=f"Card payment received for {invoice.invoice_number} — {invoice.customer.name}",
                reference=invoice.invoice_number, source="invoice_payment", created_by=None,
                lines=[
                    JournalLine(organization_id=org_id, account_id=settings.bank_account_id,
                                debit=invoice.total, credit=0, exchange_rate=1.0,
                                description=f"Paystack: {reference}"),
                    JournalLine(organization_id=org_id, account_id=ar.id, debit=0, credit=invoice.total,
                                description=f"Payment: {invoice.invoice_number}"),
                ])
            db.session.add(entry)
            db.session.flush()
            pay.journal_entry_id = entry.id
            invoice.payment_journal_entry_id = entry.id
            invoice.status = "paid"
            invoice.paid_date = date.today()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return "already"
    finally:
        set_tenant(org_id)
    return status


def confirm_claim(payment, user_id, bank_account):
    """Admin accepts a transfer claim: post Dr bank / Cr AR, mark paid."""
    invoice = Invoice.query.filter_by(id=payment.invoice_id).with_for_update().first()
    if payment.status != "claimed" or invoice.status != "sent":
        return False
    ar = Account.query.filter_by(code="2300").first()
    entry = JournalEntry(
        organization_id=payment.organization_id, entry_date=date.today(),
        memo=f"Transfer received for {invoice.invoice_number} — {invoice.customer.name}",
        reference=invoice.invoice_number, source="invoice_payment", created_by=user_id,
        lines=[
            JournalLine(organization_id=payment.organization_id, account_id=bank_account.id,
                        debit=invoice.total, credit=0, exchange_rate=1.0,
                        description=f"Transfer: {invoice.invoice_number}"),
            JournalLine(organization_id=payment.organization_id, account_id=ar.id, debit=0,
                        credit=invoice.total, description=f"Payment: {invoice.invoice_number}"),
        ])
    db.session.add(entry)
    db.session.flush()
    payment.status = "confirmed"
    payment.journal_entry_id = entry.id
    invoice.payment_journal_entry_id = entry.id
    invoice.status = "paid"
    invoice.paid_date = date.today()
    db.session.commit()
    return True
