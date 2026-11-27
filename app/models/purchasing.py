import hashlib
import uuid
from datetime import date, datetime

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

BILL_STATUSES = ["draft", "open", "paid", "void"]

PO_STATUSES = [
    "draft",
    "pending_finance_review",
    "pending_director_approval",
    "approved_committed",
    "rejected",
    "fulfilled",
    "cancelled",
]

PO_STATUS_LABELS = {
    "draft": "Draft",
    "pending_finance_review": "Pending Finance Review",
    "pending_director_approval": "Pending Director Approval",
    "approved_committed": "Approved & Committed",
    "rejected": "Rejected",
    "fulfilled": "Fulfilled / Invoiced",
    "cancelled": "Cancelled",
}

PO_STATUS_BADGES = {
    "draft": "bg-secondary",
    "pending_finance_review": "bg-warning text-dark",
    "pending_director_approval": "bg-info text-dark",
    "approved_committed": "bg-success",
    "rejected": "bg-danger",
    "fulfilled": "bg-primary",
    "cancelled": "bg-dark",
}


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


# ---------------------------------------------------------------------------
# Purchase Orders & Encumbrance / Commitment Accounting
# ---------------------------------------------------------------------------

class PurchaseOrder(TenantScopedMixin, db.Model):
    """A Purchase Order representing an encumbrance/commitment against a
    specific grant or project budget before the vendor bill is received.
    Enforces strict segregation of duties across Maker-Checker tiers:
    Program Officers prepare, Finance Managers review (and approve within
    delegated limits or escalate), and Directors approve above the threshold.
    Approved POs immediately reserve project budget (encumbrance) to prevent
    grant budget overspends."""
    __tablename__ = "purchase_orders"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    po_number = db.Column(db.String(30), nullable=False)
    vendor_id = db.Column(UUID(as_uuid=True), db.ForeignKey("vendors.id"), nullable=False)
    project_id = db.Column(UUID(as_uuid=True), db.ForeignKey("projects.id"), nullable=False)
    po_date = db.Column(db.Date, nullable=False, default=date.today)
    delivery_date = db.Column(db.Date)
    reference = db.Column(db.String(100))
    notes = db.Column(db.Text)
    amount = db.Column(db.Float, default=0.0, nullable=False)
    status = db.Column(db.String(30), default="draft", nullable=False)

    # Segregation of duties tracking
    preparer_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    reviewer_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    approver_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    submitted_at = db.Column(db.DateTime)
    reviewed_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.Text)

    # Link to resulting Vendor Bill once fulfilled
    bill_id = db.Column(UUID(as_uuid=True), db.ForeignKey("bills.id"), nullable=True)

    vendor = db.relationship("Vendor")
    project = db.relationship("Project")
    preparer = db.relationship("User", foreign_keys=[preparer_id])
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])
    approver = db.relationship("User", foreign_keys=[approver_id])
    bill = db.relationship("Bill", foreign_keys=[bill_id])

    lines = db.relationship("POLine", backref="purchase_order", cascade="all, delete-orphan", lazy=True)
    audit_trail = db.relationship("POAuditTrail", backref="purchase_order", cascade="all, delete-orphan",
                                  order_by="POAuditTrail.timestamp.asc()", lazy=True)

    __table_args__ = (db.UniqueConstraint("organization_id", "po_number", name="uq_po_org_number"),)

    @property
    def is_encumbered(self):
        """Whether this PO actively reserves project budget funds."""
        return self.status == "approved_committed"

    @property
    def status_label(self):
        return PO_STATUS_LABELS.get(self.status, self.status)

    @property
    def status_badge(self):
        return PO_STATUS_BADGES.get(self.status, "bg-secondary")

    @property
    def total(self):
        if self.lines:
            return round(sum(l.amount for l in self.lines), 2)
        return round(self.amount or 0.0, 2)

    def verify_audit_trail(self):
        """Verifies the SHA-256 cryptographic chain of all audit entries for this PO."""
        entries = self.audit_trail
        if not entries:
            return True, "No audit records found"
        prev_hash = "0" * 64
        for idx, entry in enumerate(entries):
            if entry.previous_hash != prev_hash:
                return False, f"Broken chain at step {idx + 1}: expected prev {prev_hash[:8]}..., found {entry.previous_hash[:8]}..."
            expected_hash = POAuditTrail.compute_hash(
                prev_hash=prev_hash,
                po_id=str(self.id),
                actor_id=str(entry.actor_id) if entry.actor_id else "",
                action=entry.action,
                timestamp_str=entry.timestamp.strftime("%Y-%m-%d %H:%M:%S") if entry.timestamp else "",
                details=entry.details or "",
            )
            if entry.current_hash != expected_hash:
                return False, f"Tampered entry at step {idx + 1} ({entry.action}): hash mismatch"
            prev_hash = entry.current_hash
        return True, "All audit signatures cryptographically valid"


class POLine(TenantScopedMixin, db.Model):
    """An itemized line on a Purchase Order charged to an expense or asset category."""
    __tablename__ = "po_lines"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    po_id = db.Column(UUID(as_uuid=True), db.ForeignKey("purchase_orders.id"), nullable=False)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=False)
    description = db.Column(db.String(300))
    amount = db.Column(db.Float, default=0.0)

    account = db.relationship("Account")


class POAuditTrail(TenantScopedMixin, db.Model):
    """Cryptographically chained (SHA-256) tamper-proof audit trail for Purchase Orders.
    Guarantees that no preparation, review, escalation, approval, or rejection can be
    retroactively modified without breaking the chain."""
    __tablename__ = "po_audit_trails"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    po_id = db.Column(UUID(as_uuid=True), db.ForeignKey("purchase_orders.id"), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    actor_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    details = db.Column(db.Text)
    previous_hash = db.Column(db.String(64), nullable=False)
    current_hash = db.Column(db.String(64), nullable=False)

    actor = db.relationship("User")

    @staticmethod
    def compute_hash(prev_hash, po_id, actor_id, action, timestamp_str, details):
        payload = f"{prev_hash}|{po_id}|{actor_id}|{action}|{timestamp_str}|{details}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def next_po_number(organization_id):
    """Sequential per-org purchase order numbers (PO-0001, PO-0002, ...)."""
    count = PurchaseOrder.query.filter_by(organization_id=organization_id).count()
    return f"PO-{count + 1:04d}"
