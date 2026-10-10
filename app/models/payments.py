import secrets
import uuid

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin


class PaymentSettings(TenantScopedMixin, db.Model):
    """How an organisation gets paid on invoices. One row per org
    (RLS-secured). The Paystack secret key is stored ENCRYPTED
    (app/payments/crypto.py) and never rendered back to the browser --
    only `paystack_key_hint` (e.g. 'sk_live_...a1b2') is shown."""
    __tablename__ = "payment_settings"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    paystack_secret_enc = db.Column(db.Text)
    paystack_key_hint = db.Column(db.String(40))
    # The ledger account that card payments are posted into (Dr bank /
    # Cr Accounts Receivable), and whose account number/branch are shown
    # on the invoice page for bank transfers. Must be an NGN bank account.
    bank_account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=True)
    show_bank_details = db.Column(db.Boolean, default=True, nullable=False)
    bank_instructions = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    bank_account = db.relationship("Account")

    __table_args__ = (db.UniqueConstraint("organization_id", name="uq_payment_settings_org"),)

    @property
    def has_paystack_key(self):
        return bool(self.paystack_secret_enc)

    @property
    def card_enabled(self):
        return self.has_paystack_key and self.bank_account_id is not None


class InvoicePayment(TenantScopedMixin, db.Model):
    """A payment event against an invoice: a card payment via the
    organisation's Paystack, or a customer's "I've paid by transfer"
    claim awaiting the organisation's confirmation.

    status: 'paid' (card, posted to the books) | 'claimed' (transfer
    claim, awaiting confirmation) | 'confirmed' (claim accepted and
    recorded) | 'dismissed' (claim rejected) | 'duplicate' (a second
    payment arrived after the invoice was already paid -- needs a
    refund, NOT posted) | 'mismatch' (amount differed from the invoice
    total -- NOT posted, needs a manual look)."""
    __tablename__ = "invoice_payments"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = db.Column(UUID(as_uuid=True), db.ForeignKey("invoices.id"), nullable=False, index=True)
    reference = db.Column(db.String(100), nullable=False, unique=True)
    method = db.Column(db.String(20), nullable=False)       # 'card' | 'transfer'
    status = db.Column(db.String(20), nullable=False)
    amount_minor = db.Column(db.BigInteger)
    payer_name = db.Column(db.String(150))
    payer_email = db.Column(db.String(150))
    note = db.Column(db.String(500))
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    journal_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)


def new_share_token():
    return secrets.token_urlsafe(32)


class InvoiceShareLink(db.Model):
    """Maps an unguessable public token to one invoice.

    Deliberately NOT row-secured (same reasoning as `users`, see
    scripts/setup_rls.sql): the public invoice page has no login and
    therefore no tenant context, so it must be able to look up WHICH
    tenant a token belongs to before it can set that context. The row
    reveals only (organization_id, invoice_id), both random UUIDs; every
    read of actual invoice data afterwards happens under RLS with that
    tenant set. The token is 256 bits of randomness, so it can't be
    guessed or enumerated."""
    __tablename__ = "invoice_share_links"
    token = db.Column(db.String(64), primary_key=True, default=new_share_token)
    organization_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organizations.id"), nullable=False, index=True)
    invoice_id = db.Column(UUID(as_uuid=True), db.ForeignKey("invoices.id"), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
