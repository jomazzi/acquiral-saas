from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import Account, JournalEntry, JournalLine
from app.models.invoicing import Customer, Invoice, InvoiceLine, next_invoice_number, DEFAULT_VAT_RATE
from app.pdf.invoice_pdf import generate_invoice_pdf

invoicing_bp = Blueprint("invoicing", __name__)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@invoicing_bp.route("/customers", methods=["GET", "POST"])
@login_required
def customers_list():
    if request.method == "POST":
        name = request.form["name"].strip()
        if name and not Customer.query.filter_by(name=name).first():
            db.session.add(Customer(
                organization_id=current_user.organization_id,
                name=name, email=request.form.get("email", "").strip(),
                phone=request.form.get("phone", "").strip(),
                address=request.form.get("address", "").strip(),
                notes=request.form.get("notes", "").strip(),
            ))
            db.session.commit()
            flash("Customer added.", "success")
        else:
            flash("Enter a name; it must not already exist.", "error")
        return redirect(url_for("invoicing.customers_list"))
    customers = Customer.query.order_by(Customer.name).all()
    return render_template("invoicing/customers.html", customers=customers)


@invoicing_bp.route("/customers/<uuid:customer_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def customer_edit(customer_id):
    customer = Customer.query.filter_by(id=customer_id).first_or_404()
    if request.method == "POST":
        customer.name = request.form["name"].strip()
        customer.email = request.form.get("email", "").strip()
        customer.phone = request.form.get("phone", "").strip()
        customer.address = request.form.get("address", "").strip()
        customer.notes = request.form.get("notes", "").strip()
        customer.active = bool(request.form.get("active"))
        db.session.commit()
        flash("Customer updated.", "success")
        return redirect(url_for("invoicing.customers_list"))
    return render_template("invoicing/customer_edit.html", customer=customer)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@invoicing_bp.route("/invoices")
@login_required
def invoices_list():
    invoices = Invoice.query.order_by(Invoice.issue_date.desc(), Invoice.invoice_number.desc()).all()
    return render_template("invoicing/invoices.html", invoices=invoices)


@invoicing_bp.route("/invoices/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    customers = Customer.query.filter_by(active=True).order_by(Customer.name).all()
    if not customers:
        flash("Add a customer before creating an invoice.", "error")
        return redirect(url_for("invoicing.customers_list"))

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        issue_date = datetime.strptime(request.form["issue_date"], "%Y-%m-%d").date()
        due_date_raw = request.form.get("due_date") or None
        due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date() if due_date_raw else None
        vat_rate = float(request.form.get("vat_rate") or 0)
        notes = request.form.get("notes", "").strip()

        descriptions = request.form.getlist("description[]")
        quantities = request.form.getlist("quantity[]")
        unit_prices = request.form.getlist("unit_price[]")

        lines = []
        for desc, qty, price in zip(descriptions, quantities, unit_prices):
            desc = desc.strip()
            if not desc:
                continue
            qty_val = float(qty or 0)
            price_val = float(price or 0)
            if qty_val <= 0 or price_val < 0:
                continue
            lines.append(InvoiceLine(
                organization_id=current_user.organization_id,
                description=desc, quantity=qty_val, unit_price=price_val,
            ))

        if not customer_id:
            flash("Choose a customer.", "error")
        elif not lines:
            flash("Add at least one line item with a description and quantity.", "error")
        else:
            invoice = Invoice(
                organization_id=current_user.organization_id,
                invoice_number=next_invoice_number(current_user.organization_id),
                customer_id=customer_id, issue_date=issue_date, due_date=due_date,
                vat_rate=vat_rate, notes=notes, status="draft",
                created_by=current_user.id, lines=lines,
            )
            db.session.add(invoice)
            db.session.commit()
            flash(f"Invoice {invoice.invoice_number} created as a draft.", "success")
            return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice.id))

    return render_template("invoicing/invoice_form.html", customers=customers,
                            today=date.today().isoformat(), default_vat_rate=DEFAULT_VAT_RATE)


@invoicing_bp.route("/invoices/<uuid:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id).first_or_404()
    bank_accounts = Account.query.filter_by(is_cash_or_bank=True, active=True).order_by(Account.code).all()
    from app.models.payments import InvoicePayment
    from app.payments import service as pay_service
    pay_url, payments = None, []
    if invoice.status in ("sent", "paid"):
        pay_url = url_for("pay.public_invoice", token=pay_service.ensure_share_link(invoice).token, _external=True)
        payments = InvoicePayment.query.filter_by(invoice_id=invoice.id).order_by(InvoicePayment.created_at).all()
    return render_template("invoicing/invoice_detail.html", invoice=invoice, bank_accounts=bank_accounts,
                           pay_url=pay_url, payments=payments)


@invoicing_bp.route("/invoices/<uuid:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    invoice = Invoice.query.filter_by(id=invoice_id).first_or_404()
    from app.payments import service as pay_service
    pay_url = None
    if invoice.status == "sent":
        pay_url = url_for("pay.public_invoice", token=pay_service.ensure_share_link(invoice).token, _external=True)
    pdf_buf = generate_invoice_pdf(invoice, pay_url=pay_url)
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True,
                      download_name=f"{invoice.invoice_number}.pdf")


@invoicing_bp.route("/invoices/<uuid:invoice_id>/send", methods=["POST"])
@login_required
def invoice_send(invoice_id):
    """Marks the invoice sent and posts it to the books: Dr Accounts
    Receivable, Cr Service/Invoice Income (+ Cr VAT Payable if a VAT
    rate applies). Deliberately separate from invoice creation -- a
    draft invoice can be edited/discarded freely with no accounting
    impact; only a sent invoice hits the general ledger."""
    invoice = Invoice.query.filter_by(id=invoice_id).first_or_404()
    if invoice.status != "draft":
        flash("Only a draft invoice can be sent.", "error")
        return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))
    if not invoice.lines:
        flash("Cannot send an invoice with no line items.", "error")
        return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))

    ar_account = Account.query.filter_by(code="2300").first()
    income_account = Account.query.filter_by(code="4200").first()
    vat_account = Account.query.filter_by(code="2230").first()

    org_id = current_user.organization_id
    lines = [
        JournalLine(organization_id=org_id, account_id=ar_account.id, debit=invoice.total, credit=0,
                    description=f"Invoice {invoice.invoice_number} — {invoice.customer.name}"),
        JournalLine(organization_id=org_id, account_id=income_account.id, debit=0, credit=invoice.subtotal,
                    description=f"Invoice {invoice.invoice_number} — {invoice.customer.name}"),
    ]
    if invoice.vat_amount and vat_account:
        lines.append(JournalLine(organization_id=org_id, account_id=vat_account.id, debit=0, credit=invoice.vat_amount,
                                  description=f"VAT on {invoice.invoice_number}"))

    entry = JournalEntry(organization_id=org_id, entry_date=invoice.issue_date,
                          memo=f"Invoice {invoice.invoice_number} to {invoice.customer.name}",
                          reference=invoice.invoice_number, source="invoice",
                          created_by=current_user.id, lines=lines)
    db.session.add(entry)
    db.session.flush()
    invoice.sent_journal_entry_id = entry.id
    invoice.status = "sent"
    db.session.commit()
    flash(f"Invoice {invoice.invoice_number} sent and posted to the journal.", "success")
    return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))


@invoicing_bp.route("/invoices/<uuid:invoice_id>/record-payment", methods=["POST"])
@login_required
def invoice_record_payment(invoice_id):
    """Posts Dr Bank/Cash, Cr Accounts Receivable -- the same
    unrounded-intermediate-amount rule as elsewhere applies if the
    receiving account isn't NGN."""
    invoice = Invoice.query.filter_by(id=invoice_id).first_or_404()
    if invoice.status != "sent":
        flash("Only a sent (unpaid) invoice can be marked paid.", "error")
        return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))

    bank_acc = Account.query.filter_by(id=request.form.get("bank_account_id")).first()
    if not bank_acc or not bank_acc.is_cash_or_bank:
        flash("Choose which account received the payment.", "error")
        return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))

    rate = 1.0
    if bank_acc.currency != "NGN":
        rate = request.form.get("exchange_rate", type=float) or 0
        if rate <= 0:
            flash(f"Enter the Naira exchange rate for {bank_acc.name} ({bank_acc.currency}).", "error")
            return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))

    ar_account = Account.query.filter_by(code="2300").first()
    native_amount = (invoice.total / rate) if rate else invoice.total

    org_id = current_user.organization_id
    entry = JournalEntry(
        organization_id=org_id, entry_date=date.today(),
        memo=f"Payment received for {invoice.invoice_number} — {invoice.customer.name}",
        reference=invoice.invoice_number, source="invoice_payment", created_by=current_user.id,
        lines=[
            JournalLine(organization_id=org_id, account_id=bank_acc.id, debit=native_amount, credit=0,
                        exchange_rate=rate, description=f"Payment: {invoice.invoice_number}"),
            JournalLine(organization_id=org_id, account_id=ar_account.id, debit=0, credit=invoice.total,
                        description=f"Payment: {invoice.invoice_number}"),
        ],
    )
    db.session.add(entry)
    db.session.flush()
    invoice.payment_journal_entry_id = entry.id
    invoice.status = "paid"
    invoice.paid_date = date.today()
    db.session.commit()
    flash(f"Invoice {invoice.invoice_number} marked as paid.", "success")
    return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))


@invoicing_bp.route("/invoices/<uuid:invoice_id>/void", methods=["POST"])
@login_required
@admin_required
def invoice_void(invoice_id):
    """Only a still-draft invoice can be voided here -- it has no
    journal postings yet, so there's nothing to reverse. A sent or paid
    invoice already has real accounting entries; undoing those needs a
    deliberate reversing journal entry, not a status flip, so that's
    left to the Journal module rather than silently orphaning postings."""
    invoice = Invoice.query.filter_by(id=invoice_id).first_or_404()
    if invoice.status != "draft":
        flash("Only a draft invoice can be voided here — a sent or paid invoice already has journal postings; "
              "reverse those from the Journal instead.", "error")
        return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))
    invoice.status = "void"
    db.session.commit()
    flash(f"Invoice {invoice.invoice_number} voided.", "success")
    return redirect(url_for("invoicing.invoice_detail", invoice_id=invoice_id))
