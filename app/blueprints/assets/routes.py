from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import Account, Project, JournalEntry, JournalLine
from app.models.assets import FixedAsset, ASSET_CATEGORIES, ASSET_STATUSES

assets_bp = Blueprint("assets", __name__, url_prefix="/assets")


@assets_bp.route("")
@login_required
def assets_list():
    assets = FixedAsset.query.order_by(FixedAsset.acquisition_date.desc()).all()
    rows = []
    total_cost = total_nbv = total_acc_dep = 0.0
    for a in assets:
        acc_dep = a.accumulated_depreciation()
        nbv = a.net_book_value()
        rows.append({"asset": a, "acc_dep": acc_dep, "nbv": nbv})
        if a.status != "Disposed":
            total_cost += a.cost
            total_nbv += nbv
            total_acc_dep += acc_dep
    return render_template("assets/assets.html", rows=rows, total_cost=total_cost, total_nbv=total_nbv,
                            total_acc_dep=total_acc_dep, today=date.today().isoformat())


@assets_bp.route("/new", methods=["GET", "POST"])
@login_required
def asset_new():
    all_projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    bank_accounts = Account.query.filter_by(is_cash_or_bank=True, active=True).order_by(Account.code).all()
    if request.method == "POST":
        name = request.form["name"].strip()
        category = request.form["category"]
        cost = float(request.form.get("cost") or 0)
        salvage_value = float(request.form.get("salvage_value") or 0)
        useful_life_years = int(request.form.get("useful_life_years") or 5)
        acquisition_date = datetime.strptime(request.form["acquisition_date"], "%Y-%m-%d").date()
        project_id = request.form.get("project_id") or None
        serial_number = request.form.get("serial_number", "").strip()
        custodian = request.form.get("custodian", "").strip()
        location = request.form.get("location", "").strip()
        paid_from = request.form.get("paid_from")  # account id, 'payable', or 'none'

        rate = 1.0
        credit_account = None
        if paid_from == "payable":
            credit_account = Account.query.filter_by(code="2000").first()
        elif paid_from and paid_from != "none":
            credit_account = Account.query.filter_by(id=paid_from).first()
            if not credit_account or not credit_account.is_cash_or_bank:
                flash("Choose a valid account to pay from.", "error")
                return render_template("assets/asset_form.html", categories=ASSET_CATEGORIES.keys(), projects=all_projects,
                                        bank_accounts=bank_accounts, today=date.today().isoformat())
            if credit_account.currency != "NGN":
                rate = request.form.get("exchange_rate", type=float) or 0
                if rate <= 0:
                    flash(f"Enter the Naira exchange rate for {credit_account.name} ({credit_account.currency}).", "error")
                    return render_template("assets/asset_form.html", categories=ASSET_CATEGORIES.keys(), projects=all_projects,
                                            bank_accounts=bank_accounts, today=date.today().isoformat())

        asset = FixedAsset(
            organization_id=current_user.organization_id,
            name=name, category=category, cost=cost, salvage_value=salvage_value,
            useful_life_years=useful_life_years, acquisition_date=acquisition_date,
            project_id=project_id if project_id else None,
            serial_number=serial_number, custodian=custodian, location=location,
            last_depreciated_on=acquisition_date,
        )
        db.session.add(asset)
        db.session.flush()

        if credit_account and cost > 0:
            # FixedAsset.cost is always recorded in Naira, so if paid from a
            # foreign-currency account, the native amount actually debited
            # is cost / rate. Not rounded here -- rounding this
            # intermediate value before multiplying back by rate for
            # credit_base would introduce a mismatch against the NGN cost
            # above (the same fix ported from the single-tenant app's
            # journal-entry logic). Round only the final compared totals.
            native_amount = (cost / rate) if rate else cost
            asset_account = Account.query.filter_by(code=ASSET_CATEGORIES[category]).first()
            entry = JournalEntry(
                organization_id=current_user.organization_id,
                entry_date=acquisition_date,
                memo=f"Acquisition of fixed asset: {name}",
                reference=serial_number, source="asset", created_by=current_user.id,
                lines=[
                    JournalLine(organization_id=current_user.organization_id,
                                account_id=asset_account.id, project_id=asset.project_id,
                                debit=cost, credit=0, description=f"Purchase of {name}"),
                    JournalLine(organization_id=current_user.organization_id,
                                account_id=credit_account.id, project_id=asset.project_id,
                                debit=0, credit=native_amount, exchange_rate=rate,
                                description=f"Payment for {name}" if paid_from != "payable" else f"Payable for {name}"),
                ],
            )
            db.session.add(entry)
            db.session.flush()
            asset.acquisition_entry_id = entry.id

        db.session.commit()
        flash("Fixed asset added.", "success")
        return redirect(url_for("assets.assets_list"))

    return render_template("assets/asset_form.html", categories=ASSET_CATEGORIES.keys(), projects=all_projects,
                            bank_accounts=bank_accounts, today=date.today().isoformat())


@assets_bp.route("/<uuid:asset_id>/dispose", methods=["POST"])
@login_required
@admin_required
def asset_dispose(asset_id):
    asset = FixedAsset.query.filter_by(id=asset_id).first_or_404()
    disposal_date = datetime.strptime(request.form["disposal_date"], "%Y-%m-%d").date()
    disposal_value = float(request.form.get("disposal_value") or 0)
    asset.status = "Disposed"
    asset.disposal_date = disposal_date
    asset.disposal_value = disposal_value
    db.session.commit()
    flash(f"{asset.name} marked as disposed.", "success")
    return redirect(url_for("assets.assets_list"))


@assets_bp.route("/run-depreciation", methods=["POST"])
@login_required
def run_depreciation():
    as_of = datetime.strptime(request.form["as_of"], "%Y-%m-%d").date()
    assets = FixedAsset.query.filter(FixedAsset.status != "Disposed").all()

    lines = []
    total = 0.0
    for a in assets:
        prior = a.accumulated_depreciation(a.last_depreciated_on) if a.last_depreciated_on else 0.0
        current = a.accumulated_depreciation(as_of)
        delta = round(current - prior, 2)
        if delta > 0:
            asset_account = Account.query.filter_by(code=ASSET_CATEGORIES[a.category]).first()
            lines.append((a, asset_account, delta))
            total += delta
            a.last_depreciated_on = as_of

    if not lines:
        flash("No depreciation to post for that date (already up to date, or no depreciable assets).", "error")
        return redirect(url_for("assets.assets_list"))

    dep_expense = Account.query.filter_by(code="5850").first()
    acc_dep = Account.query.filter_by(code="1580").first()

    journal_lines = [
        JournalLine(organization_id=current_user.organization_id,
                    account_id=dep_expense.id, project_id=a.project_id, debit=delta, credit=0,
                    description=f"Depreciation: {a.name}")
        for a, _, delta in lines
    ] + [
        JournalLine(organization_id=current_user.organization_id,
                    account_id=acc_dep.id, project_id=a.project_id, debit=0, credit=delta,
                    description=f"Accumulated depreciation: {a.name}")
        for a, _, delta in lines
    ]

    entry = JournalEntry(organization_id=current_user.organization_id,
                          entry_date=as_of, memo=f"Depreciation run to {as_of}", source="depreciation",
                          created_by=current_user.id, lines=journal_lines)
    db.session.add(entry)
    db.session.commit()
    flash(f"Posted depreciation of N{total:,.2f} across {len(lines)} asset(s).", "success")
    return redirect(url_for("assets.assets_list"))


@assets_bp.route("/reports/asset-register")
@login_required
def report_asset_register():
    assets = FixedAsset.query.order_by(FixedAsset.category, FixedAsset.name).all()
    rows = [{"asset": a, "acc_dep": a.accumulated_depreciation(), "nbv": a.net_book_value()} for a in assets]
    total_cost = sum(r["asset"].cost for r in rows if r["asset"].status != "Disposed")
    total_nbv = sum(r["nbv"] for r in rows if r["asset"].status != "Disposed")
    return render_template("assets/report_asset_register.html", rows=rows, total_cost=total_cost, total_nbv=total_nbv)
