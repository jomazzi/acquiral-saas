from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import Account, JournalEntry, JournalLine
from app.models.purchasing import Vendor, Bill, BillLine, next_bill_number

purchasing_bp = Blueprint("purchasing", __name__)


def _expense_accounts():
    """Categories a bill line can be charged to -- Expense accounts, plus
    non-cash Asset accounts (e.g. Office Equipment) for a bill representing
    an equipment purchase on credit. Deliberately excludes cash/bank
    accounts: those are what a bill gets PAID FROM, not charged to."""
    return (Account.query
            .filter(Account.type.in_(["Expense", "Asset"]))
            .filter_by(active=True, is_cash_or_bank=False)
            .order_by(Account.code).all())


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------

@purchasing_bp.route("/vendors", methods=["GET", "POST"])
@login_required
def vendors_list():
    if request.method == "POST":
        name = request.form["name"].strip()
        if name and not Vendor.query.filter_by(name=name).first():
            db.session.add(Vendor(
                organization_id=current_user.organization_id,
                name=name, email=request.form.get("email", "").strip(),
                phone=request.form.get("phone", "").strip(),
                address=request.form.get("address", "").strip(),
                notes=request.form.get("notes", "").strip(),
            ))
            db.session.commit()
            flash("Vendor added.", "success")
        else:
            flash("Enter a name; it must not already exist.", "error")
        return redirect(url_for("purchasing.vendors_list"))
    vendors = Vendor.query.order_by(Vendor.name).all()
    return render_template("purchasing/vendors.html", vendors=vendors)


@purchasing_bp.route("/vendors/<uuid:vendor_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def vendor_edit(vendor_id):
    vendor = Vendor.query.filter_by(id=vendor_id).first_or_404()
    if request.method == "POST":
        vendor.name = request.form["name"].strip()
        vendor.email = request.form.get("email", "").strip()
        vendor.phone = request.form.get("phone", "").strip()
        vendor.address = request.form.get("address", "").strip()
        vendor.notes = request.form.get("notes", "").strip()
        vendor.active = bool(request.form.get("active"))
        db.session.commit()
        flash("Vendor updated.", "success")
        return redirect(url_for("purchasing.vendors_list"))
    return render_template("purchasing/vendor_edit.html", vendor=vendor)


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------

@purchasing_bp.route("/bills")
@login_required
def bills_list():
    bills = Bill.query.order_by(Bill.bill_date.desc(), Bill.bill_number.desc()).all()
    outstanding_total = sum(b.total for b in bills if b.status == "open")
    return render_template("purchasing/bills.html", bills=bills, outstanding_total=outstanding_total)


@purchasing_bp.route("/bills/new", methods=["GET", "POST"])
@login_required
def bill_new():
    vendors = Vendor.query.filter_by(active=True).order_by(Vendor.name).all()
    expense_accounts = _expense_accounts()
    if not vendors:
        flash("Add a vendor before entering a bill.", "error")
        return redirect(url_for("purchasing.vendors_list"))

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        bill_date = datetime.strptime(request.form["bill_date"], "%Y-%m-%d").date()
        due_date_raw = request.form.get("due_date") or None
        due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date() if due_date_raw else None
        reference = request.form.get("reference", "").strip()
        notes = request.form.get("notes", "").strip()

        descriptions = request.form.getlist("description[]")
        account_ids = request.form.getlist("account_id[]")
        amounts = request.form.getlist("amount[]")

        lines = []
        for desc, acct_id, amt in zip(descriptions, account_ids, amounts):
            if not acct_id:
                continue
            amt_val = float(amt or 0)
            if amt_val <= 0:
                continue
            lines.append(BillLine(
                organization_id=current_user.organization_id,
                account_id=acct_id, description=desc.strip(), amount=amt_val,
            ))

        if not vendor_id:
            flash("Choose a vendor.", "error")
        elif not lines:
            flash("Add at least one line with a category and amount.", "error")
        else:
            bill = Bill(
                organization_id=current_user.organization_id,
                bill_number=next_bill_number(current_user.organization_id),
                vendor_id=vendor_id, bill_date=bill_date, due_date=due_date,
                reference=reference, notes=notes, status="draft",
                created_by=current_user.id, lines=lines,
            )
            db.session.add(bill)
            db.session.commit()
            flash(f"Bill {bill.bill_number} created as a draft.", "success")
            return redirect(url_for("purchasing.bill_detail", bill_id=bill.id))

    return render_template("purchasing/bill_form.html", vendors=vendors, expense_accounts=expense_accounts,
                            today=date.today().isoformat())


@purchasing_bp.route("/bills/<uuid:bill_id>")
@login_required
def bill_detail(bill_id):
    bill = Bill.query.filter_by(id=bill_id).first_or_404()
    bank_accounts = Account.query.filter_by(is_cash_or_bank=True, active=True).order_by(Account.code).all()
    return render_template("purchasing/bill_detail.html", bill=bill, bank_accounts=bank_accounts)


@purchasing_bp.route("/bills/<uuid:bill_id>/approve", methods=["POST"])
@login_required
def bill_approve(bill_id):
    """Marks the bill approved and posts it to the books: Dr the expense
    (or asset) account(s) on each line, Cr Accounts Payable for the
    total. Deliberately separate from bill creation -- a draft bill can
    be edited/discarded freely with no accounting impact; only an
    approved bill hits the general ledger and becomes an outstanding
    payable, the same draft/posted split used for invoices."""
    bill = Bill.query.filter_by(id=bill_id).first_or_404()
    if bill.status != "draft":
        flash("Only a draft bill can be approved.", "error")
        return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))
    if not bill.lines:
        flash("Cannot approve a bill with no line items.", "error")
        return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))

    ap_account = Account.query.filter_by(code="2000").first()
    org_id = current_user.organization_id

    lines = [
        JournalLine(organization_id=org_id, account_id=l.account_id, debit=l.amount, credit=0,
                    description=f"Bill {bill.bill_number} — {bill.vendor.name}: {l.description or l.account.name}")
        for l in bill.lines
    ]
    lines.append(JournalLine(organization_id=org_id, account_id=ap_account.id, debit=0, credit=bill.total,
                              description=f"Bill {bill.bill_number} — {bill.vendor.name}"))

    entry = JournalEntry(organization_id=org_id, entry_date=bill.bill_date,
                          memo=f"Bill {bill.bill_number} from {bill.vendor.name}",
                          reference=bill.reference or bill.bill_number, source="bill",
                          created_by=current_user.id, lines=lines)
    db.session.add(entry)
    db.session.flush()
    bill.approved_journal_entry_id = entry.id
    bill.status = "open"
    db.session.commit()
    flash(f"Bill {bill.bill_number} approved and posted to the journal.", "success")
    return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))


@purchasing_bp.route("/bills/<uuid:bill_id>/pay", methods=["POST"])
@login_required
def bill_pay(bill_id):
    """Posts Dr Accounts Payable, Cr Bank/Cash -- the mirror image of
    invoice_record_payment, with the same non-NGN exchange-rate handling."""
    bill = Bill.query.filter_by(id=bill_id).first_or_404()
    if bill.status != "open":
        flash("Only an approved (open) bill can be paid.", "error")
        return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))

    bank_acc = Account.query.filter_by(id=request.form.get("bank_account_id")).first()
    if not bank_acc or not bank_acc.is_cash_or_bank:
        flash("Choose which account to pay from.", "error")
        return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))

    rate = 1.0
    if bank_acc.currency != "NGN":
        rate = request.form.get("exchange_rate", type=float) or 0
        if rate <= 0:
            flash(f"Enter the Naira exchange rate for {bank_acc.name} ({bank_acc.currency}).", "error")
            return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))

    ap_account = Account.query.filter_by(code="2000").first()
    native_amount = (bill.total / rate) if rate else bill.total

    org_id = current_user.organization_id
    entry = JournalEntry(
        organization_id=org_id, entry_date=date.today(),
        memo=f"Payment for {bill.bill_number} — {bill.vendor.name}",
        reference=bill.bill_number, source="bill_payment", created_by=current_user.id,
        lines=[
            JournalLine(organization_id=org_id, account_id=ap_account.id, debit=bill.total, credit=0,
                        description=f"Payment: {bill.bill_number}"),
            JournalLine(organization_id=org_id, account_id=bank_acc.id, debit=0, credit=native_amount,
                        exchange_rate=rate, description=f"Payment: {bill.bill_number}"),
        ],
    )
    db.session.add(entry)
    db.session.flush()
    bill.payment_journal_entry_id = entry.id
    bill.status = "paid"
    bill.paid_date = date.today()
    db.session.commit()
    flash(f"Bill {bill.bill_number} marked as paid.", "success")
    return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))


@purchasing_bp.route("/bills/<uuid:bill_id>/void", methods=["POST"])
@login_required
@admin_required
def bill_void(bill_id):
    """Only a still-draft bill can be voided here -- same rule and same
    rationale as invoice_void: an approved or paid bill already has real
    journal postings, so undoing it needs a deliberate reversing entry
    from the Journal module, not a silent status flip."""
    bill = Bill.query.filter_by(id=bill_id).first_or_404()
    if bill.status != "draft":
        flash("Only a draft bill can be voided here — an approved or paid bill already has journal postings; "
              "reverse those from the Journal instead.", "error")
        return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))
    bill.status = "void"
    db.session.commit()
    flash(f"Bill {bill.bill_number} voided.", "success")
    return redirect(url_for("purchasing.bill_detail", bill_id=bill_id))
