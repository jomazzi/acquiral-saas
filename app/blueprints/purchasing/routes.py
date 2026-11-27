import uuid
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import Account, JournalEntry, JournalLine, Project
from app.models.purchasing import (
    Vendor, Bill, BillLine, next_bill_number,
    PurchaseOrder, POLine, POAuditTrail, next_po_number,
    PO_STATUSES, PO_STATUS_LABELS, PO_STATUS_BADGES,
)

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


# ---------------------------------------------------------------------------
# Purchase Orders & Encumbrance Workflow Engine
# ---------------------------------------------------------------------------

def _log_po_audit(po, action, actor_id, details=""):
    """Appends a cryptographically chained SHA-256 audit entry."""
    last_entry = (
        POAuditTrail.query.filter_by(po_id=po.id)
        .order_by(POAuditTrail.timestamp.desc())
        .first()
    )
    prev_hash = last_entry.current_hash if last_entry else ("0" * 64)
    now = datetime.utcnow()
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    curr_hash = POAuditTrail.compute_hash(
        prev_hash=prev_hash,
        po_id=str(po.id),
        actor_id=str(actor_id) if actor_id else "",
        action=action,
        timestamp_str=timestamp_str,
        details=details,
    )
    audit = POAuditTrail(
        organization_id=po.organization_id,
        po_id=po.id,
        action=action,
        actor_id=actor_id,
        timestamp=now,
        details=details,
        previous_hash=prev_hash,
        current_hash=curr_hash,
    )
    db.session.add(audit)


@purchasing_bp.route("/purchase-orders")
@login_required
def purchase_orders_list():
    filter_status = request.args.get("status", "all")
    query = PurchaseOrder.query.order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.po_number.desc())

    if filter_status == "pending_review":
        query = query.filter(PurchaseOrder.status.in_(["pending_finance_review", "pending_director_approval"]))
    elif filter_status in PO_STATUSES:
        query = query.filter_by(status=filter_status)

    pos = query.all()
    all_pos = PurchaseOrder.query.all()
    total_encumbered = sum(p.total for p in all_pos if p.status == "approved_committed")
    count_pending_finance = sum(1 for p in all_pos if p.status == "pending_finance_review")
    count_pending_director = sum(1 for p in all_pos if p.status == "pending_director_approval")

    return render_template(
        "purchasing/purchase_orders.html",
        pos=pos,
        filter_status=filter_status,
        total_encumbered=total_encumbered,
        count_pending_finance=count_pending_finance,
        count_pending_director=count_pending_director,
        status_labels=PO_STATUS_LABELS,
    )


@purchasing_bp.route("/purchase-orders/new", methods=["GET", "POST"])
@login_required
def po_new():
    vendors = Vendor.query.filter_by(active=True).order_by(Vendor.name).all()
    projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    expense_accounts = _expense_accounts()

    if not vendors:
        flash("Add at least one active vendor before creating a purchase order.", "error")
        return redirect(url_for("purchasing.vendors_list"))
    if not projects:
        flash("Add at least one active project/grant before creating a purchase order.", "error")
        return redirect(url_for("accounting.projects"))

    if request.method == "POST":
        vendor_id_raw = request.form.get("vendor_id")
        project_id_raw = request.form.get("project_id")
        try:
            vendor_id = uuid.UUID(vendor_id_raw) if vendor_id_raw else None
            project_id = uuid.UUID(project_id_raw) if project_id_raw else None
        except (ValueError, AttributeError):
            vendor_id = None
            project_id = None

        po_date_raw = request.form.get("po_date")
        po_date = datetime.strptime(po_date_raw, "%Y-%m-%d").date() if po_date_raw else date.today()
        del_date_raw = request.form.get("delivery_date") or None
        delivery_date = datetime.strptime(del_date_raw, "%Y-%m-%d").date() if del_date_raw else None
        reference = request.form.get("reference", "").strip()
        notes = request.form.get("notes", "").strip()
        submit_action = request.form.get("action", "draft")  # 'draft' or 'submit'

        descriptions = request.form.getlist("description[]")
        account_ids = request.form.getlist("account_id[]")
        amounts = request.form.getlist("amount[]")

        lines = []
        total_amount = 0.0
        for desc, acct_id, amt in zip(descriptions, account_ids, amounts):
            if not acct_id:
                continue
            try:
                acct_uuid = uuid.UUID(acct_id)
            except (ValueError, AttributeError):
                continue
            try:
                amt_val = float(amt or 0)
            except ValueError:
                amt_val = 0.0
            if amt_val <= 0:
                continue
            total_amount += amt_val
            lines.append(POLine(
                organization_id=current_user.organization_id,
                account_id=acct_uuid,
                description=desc.strip(),
                amount=amt_val,
            ))

        project = Project.query.filter_by(id=project_id).first() if project_id else None
        if not vendor_id:
            flash("Please choose a vendor.", "error")
        elif not project:
            flash("Please select a valid project / grant.", "error")
        elif not lines:
            flash("Add at least one item line with category and amount.", "error")
        elif submit_action == "submit" and total_amount > project.available_budget:
            flash(
                f"Budget check failed! Project '{project.name}' has available budget of ₦{project.available_budget:,.2f}. "
                f"Requested PO amount is ₦{total_amount:,.2f} (Deficit: ₦{total_amount - project.available_budget:,.2f}).",
                "error",
            )
        else:
            status = "pending_finance_review" if submit_action == "submit" else "draft"
            po = PurchaseOrder(
                organization_id=current_user.organization_id,
                po_number=next_po_number(current_user.organization_id),
                vendor_id=vendor_id,
                project_id=project_id,
                po_date=po_date,
                delivery_date=delivery_date,
                reference=reference,
                notes=notes,
                amount=round(total_amount, 2),
                status=status,
                preparer_id=current_user.id,
                submitted_at=datetime.utcnow() if submit_action == "submit" else None,
                lines=lines,
            )
            db.session.add(po)
            db.session.flush()

            # Record cryptographic audit entry
            action_name = "SUBMITTED_FOR_REVIEW" if submit_action == "submit" else "PREPARED_DRAFT"
            action_desc = (
                f"Created and submitted PO for ₦{total_amount:,.2f} against project {project.name}"
                if submit_action == "submit"
                else f"Saved draft PO for ₦{total_amount:,.2f}"
            )
            _log_po_audit(po, action=action_name, actor_id=current_user.id, details=action_desc)

            db.session.commit()
            if submit_action == "submit":
                flash(f"Purchase Order {po.po_number} submitted for Finance Review.", "success")
            else:
                flash(f"Draft Purchase Order {po.po_number} created.", "success")
            return redirect(url_for("purchasing.po_detail", po_id=po.id))

    return render_template(
        "purchasing/po_form.html",
        vendors=vendors,
        projects=projects,
        expense_accounts=expense_accounts,
        today=date.today().isoformat(),
    )


@purchasing_bp.route("/purchase-orders/<uuid:po_id>")
@login_required
def po_detail(po_id):
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    project_summary = po.project.get_budget_summary() if po.project else None
    audit_valid, audit_msg = po.verify_audit_trail()

    can_submit = po.status == "draft" and (current_user.id == po.preparer_id or current_user.is_admin)
    can_finance_review = (
        po.status == "pending_finance_review"
        and current_user.is_finance_manager
        and current_user.id != po.preparer_id
    )
    can_director_approve = (
        po.status == "pending_director_approval"
        and current_user.is_director
        and current_user.id != po.preparer_id
    )
    can_reject = (
        po.status in ["pending_finance_review", "pending_director_approval"]
        and (current_user.is_finance_manager or current_user.is_director)
        and current_user.id != po.preparer_id
    )
    can_fulfill = po.status == "approved_committed" and (current_user.is_admin or current_user.is_finance_manager)
    can_cancel = po.status in ["draft", "pending_finance_review", "pending_director_approval", "approved_committed"] and current_user.is_admin

    return render_template(
        "purchasing/po_detail.html",
        po=po,
        project_summary=project_summary,
        audit_valid=audit_valid,
        audit_msg=audit_msg,
        can_submit=can_submit,
        can_finance_review=can_finance_review,
        can_director_approve=can_director_approve,
        can_reject=can_reject,
        can_fulfill=can_fulfill,
        can_cancel=can_cancel,
    )


@purchasing_bp.route("/purchase-orders/<uuid:po_id>/submit", methods=["POST"])
@login_required
def po_submit(po_id):
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    if po.status != "draft":
        flash("Only draft purchase orders can be submitted.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    if po.total > po.project.available_budget:
        flash(
            f"Budget Check Failed: Project '{po.project.name}' available budget is ₦{po.project.available_budget:,.2f}. "
            f"This PO requires ₦{po.total:,.2f}.",
            "error",
        )
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    po.status = "pending_finance_review"
    po.preparer_id = current_user.id
    po.submitted_at = datetime.utcnow()
    _log_po_audit(
        po,
        action="SUBMITTED_FOR_REVIEW",
        actor_id=current_user.id,
        details=f"Submitted PO for ₦{po.total:,.2f} for Finance Review.",
    )
    db.session.commit()
    flash(f"Purchase Order {po.po_number} submitted for Finance Review.", "success")
    return redirect(url_for("purchasing.po_detail", po_id=po.id))


@purchasing_bp.route("/purchase-orders/<uuid:po_id>/finance-review", methods=["POST"])
@login_required
def po_finance_review(po_id):
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    if po.status != "pending_finance_review":
        flash("This purchase order is not pending Finance review.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    if not current_user.is_finance_manager:
        flash("Only a Finance Manager or Administrator can review purchase orders.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    # Strict Segregation of Duties: Preparer cannot review/approve their own PO
    if po.preparer_id == current_user.id:
        flash("Segregation of Duties Violation: You prepared this purchase order. A different Finance Manager or Director must review it.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    # Pre-spend budget verification
    if po.total > po.project.available_budget:
        flash(f"Budget Exceeded! Project '{po.project.name}' has available budget of ₦{po.project.available_budget:,.2f}.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    user_limit = current_user.approval_limit or 0.0
    po.reviewer_id = current_user.id
    po.reviewed_at = datetime.utcnow()

    # If within limit (or user is director/admin): approve directly and encumber funds
    if current_user.can_approve(po.total):
        po.status = "approved_committed"
        po.approver_id = current_user.id
        po.approved_at = datetime.utcnow()
        _log_po_audit(
            po,
            action="FINANCE_APPROVED_COMMITTED",
            actor_id=current_user.id,
            details=f"Approved under delegated financial authority (Limit: ₦{user_limit:,.2f}). Funds reserved.",
        )
        flash(f"Purchase Order {po.po_number} approved! ₦{po.total:,.2f} encumbered against {po.project.name}.", "success")
    else:
        # Exceeds limit: escalate to Director
        po.status = "pending_director_approval"
        _log_po_audit(
            po,
            action="ESCALATED_TO_DIRECTOR",
            actor_id=current_user.id,
            details=f"PO amount ₦{po.total:,.2f} exceeds Finance Manager limit ₦{user_limit:,.2f}. Escalated to Director.",
        )
        flash(
            f"Reviewed. PO {po.po_number} amount (₦{po.total:,.2f}) exceeds your delegated limit (₦{user_limit:,.2f}). "
            "Escalated to Director for Executive Approval.",
            "info",
        )

    db.session.commit()
    return redirect(url_for("purchasing.po_detail", po_id=po.id))


@purchasing_bp.route("/purchase-orders/<uuid:po_id>/director-approve", methods=["POST"])
@login_required
def po_director_approve(po_id):
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    if po.status != "pending_director_approval":
        flash("This purchase order is not pending Director approval.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    if not current_user.is_director:
        flash("Only a Director or Executive Approver can grant executive approval.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    # Strict Segregation of Duties: Preparer cannot approve their own PO
    if po.preparer_id == current_user.id:
        flash("Segregation of Duties Violation: You prepared this purchase order and cannot approve it.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    if po.total > po.project.available_budget:
        flash(f"Budget Exceeded! Project '{po.project.name}' has available budget of ₦{po.project.available_budget:,.2f}.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    po.status = "approved_committed"
    po.approver_id = current_user.id
    po.approved_at = datetime.utcnow()
    _log_po_audit(
        po,
        action="DIRECTOR_EXECUTIVE_APPROVED",
        actor_id=current_user.id,
        details=f"Executive approval granted. ₦{po.total:,.2f} committed to project {po.project.name}.",
    )
    db.session.commit()
    flash(f"Purchase Order {po.po_number} granted Executive Approval! Funds reserved.", "success")
    return redirect(url_for("purchasing.po_detail", po_id=po.id))


@purchasing_bp.route("/purchase-orders/<uuid:po_id>/reject", methods=["POST"])
@login_required
def po_reject(po_id):
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    if po.status not in ["pending_finance_review", "pending_director_approval"]:
        flash("Only pending purchase orders can be rejected.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    if not (current_user.is_finance_manager or current_user.is_director):
        flash("You do not have permission to reject purchase orders.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    if po.preparer_id == current_user.id:
        flash("You cannot review or reject your own purchase order.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    reason = request.form.get("reason", "").strip() or "No reason provided."
    po.status = "rejected"
    po.rejection_reason = reason
    _log_po_audit(
        po,
        action="REJECTED",
        actor_id=current_user.id,
        details=f"Rejected. Reason: {reason}",
    )
    db.session.commit()
    flash(f"Purchase Order {po.po_number} rejected.", "warning")
    return redirect(url_for("purchasing.po_detail", po_id=po.id))


@purchasing_bp.route("/purchase-orders/<uuid:po_id>/convert-to-bill", methods=["POST"])
@login_required
def po_convert_to_bill(po_id):
    """Liquidates the PO encumbrance into an actual Vendor Bill in Accounts Payable."""
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    if po.status != "approved_committed":
        flash("Only approved & committed purchase orders can be converted to bills.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    bill_lines = [
        BillLine(
            organization_id=po.organization_id,
            account_id=line.account_id,
            description=f"PO {po.po_number}: {line.description or ''}".strip(),
            amount=line.amount,
        )
        for line in po.lines
    ]

    bill = Bill(
        organization_id=po.organization_id,
        bill_number=next_bill_number(po.organization_id),
        vendor_id=po.vendor_id,
        bill_date=date.today(),
        due_date=po.delivery_date,
        reference=f"PO: {po.po_number}" + (f" / {po.reference}" if po.reference else ""),
        notes=f"Converted from Purchase Order {po.po_number}. {po.notes or ''}".strip(),
        status="draft",
        created_by=current_user.id,
        lines=bill_lines,
    )
    db.session.add(bill)
    db.session.flush()

    po.bill_id = bill.id
    po.status = "fulfilled"
    _log_po_audit(
        po,
        action="FULFILLED_CONVERTED_TO_BILL",
        actor_id=current_user.id,
        details=f"Converted to Vendor Bill {bill.bill_number}. Commitment liquidated into Accounts Payable.",
    )
    db.session.commit()
    flash(f"Purchase Order {po.po_number} converted to Bill {bill.bill_number} (Draft).", "success")
    return redirect(url_for("purchasing.bill_detail", bill_id=bill.id))


@purchasing_bp.route("/purchase-orders/<uuid:po_id>/cancel", methods=["POST"])
@login_required
@admin_required
def po_cancel(po_id):
    po = PurchaseOrder.query.filter_by(id=po_id).first_or_404()
    if po.status == "fulfilled":
        flash("Fulfilled purchase orders cannot be cancelled.", "error")
        return redirect(url_for("purchasing.po_detail", po_id=po.id))

    was_encumbered = po.status == "approved_committed"
    po.status = "cancelled"
    _log_po_audit(
        po,
        action="CANCELLED",
        actor_id=current_user.id,
        details="Purchase order cancelled. Reserved funds released." if was_encumbered else "Purchase order cancelled.",
    )
    db.session.commit()
    flash(f"Purchase Order {po.po_number} cancelled. Reserved funds released.", "success")
    return redirect(url_for("purchasing.po_detail", po_id=po.id))
