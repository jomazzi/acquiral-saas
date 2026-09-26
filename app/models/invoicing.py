import uuid
from datetime import date

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

INVOICE_STATUSES = ["draft", "sent", "paid", "void"]

# Nigeria's standard VAT rate. Kept as a per-invoice field (not a
# constant) so an org can zero it out for VAT-exempt income if needed,
# rather than baking 7.5% in as the only option.
DEFAULT_VAT_RATE = 7.5


class Customer(TenantScopedMixin, db.Model):
    """A commercial/service client Acquiral invoices -- deliberately
    separate from Funder (which represents a donor/grantor an NGO's
    project income comes from, tracked via journal entries + projects,
    not invoices)."""
    __tablename__ = "customers"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)

    invoices = db.relationship("Invoice", backref="customer", lazy=True)

    __table_args__ = (db.UniqueConstraint("organization_id", "name", name="uq_customer_org_name"),)


class Invoice(TenantScopedMixin, db.Model):
    __tablename__ = "invoices"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_number = db.Column(db.String(30), nullable=False)
    customer_id = db.Column(UUID(as_uuid=True), db.ForeignKey("customers.id"), nullable=False)
    issue_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date)
    currency = db.Column(db.String(3), default="NGN")
    vat_rate = db.Column(db.Float, default=DEFAULT_VAT_RATE)
    status = db.Column(db.String(20), default="draft")
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    created_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)

    # Set when the invoice is sent (posts Dr Accounts Receivable / Cr
    # Service Income + VAT Payable) and when payment is recorded (posts
    # Dr Bank or Cash / Cr Accounts Receivable). Kept separate so a paid
    # invoice's audit trail shows both postings distinctly, the same way
    # payroll_runs.journal_entry_id works for a single posting.
    sent_journal_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)
    payment_journal_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)
    paid_date = db.Column(db.Date)

    lines = db.relationship("InvoiceLine", backref="invoice", cascade="all, delete-orphan", lazy=True)

    __table_args__ = (db.UniqueConstraint("organization_id", "invoice_number", name="uq_invoice_org_number"),)

    @property
    def subtotal(self):
        return round(sum(l.amount for l in self.lines), 2)

    @property
    def vat_amount(self):
        return round(self.subtotal * (self.vat_rate or 0) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def is_overdue(self):
        return self.status == "sent" and self.due_date and self.due_date < date.today()


class InvoiceLine(TenantScopedMixin, db.Model):
    __tablename__ = "invoice_lines"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = db.Column(UUID(as_uuid=True), db.ForeignKey("invoices.id"), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit_price = db.Column(db.Float, default=0.0)

    @property
    def amount(self):
        return round((self.quantity or 0) * (self.unit_price or 0), 2)


def next_invoice_number(organization_id):
    """Sequential per-org invoice numbers (INV-0001, INV-0002, ...).
    Counts existing invoices for this org rather than keeping a separate
    counter row -- simple and correct for this app's usage pattern (one
    admin/small team creating invoices interactively, not high-concurrency
    batch creation), with the UniqueConstraint above as a backstop against
    a genuine race."""
    count = Invoice.query.filter_by(organization_id=organization_id).count()
    return f"INV-{count + 1:04d}"
