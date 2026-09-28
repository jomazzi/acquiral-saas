import uuid
from datetime import date

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

BILL_STATUSES = ["draft", "open", "paid", "void"]


class Vendor(TenantScopedMixin, db.Model):
    """A supplier Acquiral owes money to -- the payables-side mirror of
    Customer. Deliberately separate from Funder (a donor/grantor income
    comes FROM) and from Customer (who income comes from via invoices):
    a Vendor is who money goes TO for goods/services received."""
    __tablename__ = "vendors"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)

    bills = db.relationship("Bill", backref="vendor", lazy=True)

    __table_args__ = (db.UniqueConstraint("organization_id", "name", name="uq_vendor_org_name"),)


class Bill(TenantScopedMixin, db.Model):
    """A vendor bill -- the payables-side mirror of Invoice. A draft bill
    has no accounting impact and can be edited/discarded freely; approving
    it posts Dr Expense(s) / Cr Accounts Payable (2000) and moves it to
    'open' (an outstanding payable with a due date); paying it posts
    Dr Accounts Payable / Cr Bank and moves it to 'paid'. This is what
    lets Acquiral show "what do we currently owe" between the bill date
    and the day it's actually paid, which a straight expense journal
    entry (Dr Expense / Cr Bank on the same day) cannot represent."""
    __tablename__ = "bills"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bill_number = db.Column(db.String(30), nullable=False)
    vendor_id = db.Column(UUID(as_uuid=True), db.ForeignKey("vendors.id"), nullable=False)
    bill_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date)
    reference = db.Column(db.String(100))  # vendor's own invoice/reference number
    status = db.Column(db.String(20), default="draft")
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    created_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)

    # Set when the bill is approved (posts Dr Expense(s) / Cr Accounts
    # Payable) and when payment is recorded (posts Dr Accounts Payable /
    # Cr Bank). Kept separate for the same reason as Invoice's two ids --
    # a paid bill's audit trail shows both postings distinctly.
    approved_journal_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)
    payment_journal_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)
    paid_date = db.Column(db.Date)

    lines = db.relationship("BillLine", backref="bill", cascade="all, delete-orphan", lazy=True)

    __table_args__ = (db.UniqueConstraint("organization_id", "bill_number", name="uq_bill_org_number"),)

    @property
    def total(self):
        return round(sum(l.amount for l in self.lines), 2)

    @property
    def is_overdue(self):
        return self.status == "open" and self.due_date and self.due_date < date.today()


class BillLine(TenantScopedMixin, db.Model):
    """One expense/asset category on a bill -- e.g. a single electricity
    bill might have one line against 5400 Office Rent & Utilities. Unlike
    InvoiceLine there's no quantity/unit-price breakdown: real vendor
    bills are usually a lump sum per category, not a priced line item."""
    __tablename__ = "bill_lines"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bill_id = db.Column(UUID(as_uuid=True), db.ForeignKey("bills.id"), nullable=False)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=False)
    description = db.Column(db.String(300))
    amount = db.Column(db.Float, default=0.0)

    account = db.relationship("Account")


def next_bill_number(organization_id):
    """Sequential per-org bill numbers (BILL-0001, BILL-0002, ...), same
    approach and same race-condition caveat as next_invoice_number."""
    count = Bill.query.filter_by(organization_id=organization_id).count()
    return f"BILL-{count + 1:04d}"
