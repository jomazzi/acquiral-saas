import uuid
from datetime import timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.tenant import Organization, User
from app.models.accounting import seed_default_accounts
from app.billing import plans
from app.billing.access import utcnow, user_limit

auth_bp = Blueprint("auth", __name__)


def _unique_slug(base):
    slug = base
    n = 1
    while Organization.query.filter_by(slug=slug).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Creates a brand-new tenant (Organization) plus its first admin
    User, then seeds that tenant's chart of accounts. This is the one
    place in the app that runs BEFORE any tenant context exists — there
    is nothing to scope to yet, since we're creating the tenant itself."""
    if current_user.is_authenticated:
        return redirect(url_for("accounting.dashboard"))

    if request.method == "POST":
        org_name = request.form["org_name"].strip()
        username = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        password = request.form["password"]

        if not org_name or not username or len(password) < 6:
            flash("Organization name, username and a password of at least 6 characters are required.", "error")
            return render_template("auth/signup.html")

        base_slug = "".join(c.lower() if c.isalnum() else "-" for c in org_name).strip("-") or "org"
        slug = _unique_slug(base_slug)

        org = Organization(name=org_name, slug=slug, billing_status="trialing",
                           trial_ends_at=utcnow() + timedelta(days=plans.TRIAL_DAYS))
        db.session.add(org)
        db.session.commit()  # need org.id before we can create the tenant-scoped user

        user = User(organization_id=org.id, username=username, full_name=full_name, role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        # Any query from here on that touches a tenant-scoped table needs
        # RLS context explicitly applied — the before_request hook won't
        # fire again mid-request, and the earlier commits ended whatever
        # transaction any previously-set context lived in.
        from app import set_tenant
        set_tenant(org.id)
        seed_default_accounts(org.id)

        login_user(user)
        flash(f"Welcome to Acquiral, {org_name}!", "success")
        return redirect(url_for("accounting.dashboard"))

    return render_template("auth/signup.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("accounting.dashboard"))
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        # users is NOT row-secured (see app/models/tenant.py) — a
        # username is only unique WITHIN an org, so a bare
        # filter_by(username=...) could match a same-named user in a
        # different tenant. There is no tenant context yet at login
        # time (that's the whole point of the exclusion), so disambiguate
        # by also taking the org slug/name if provided; for now (single
        # login form, MVP) we accept the first active match — a real
        # multi-org-name-collision flow would ask which org, but that's
        # a UX decision for later, not a security one, since login()
        # still requires the correct password for whichever account is
        # matched.
        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            login_user(user)
            return redirect(request.args.get("next") or url_for("accounting.dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form["current_password"]
        new_pw = request.form["new_password"]
        if not current_user.check_password(current_pw):
            flash("Current password is incorrect.", "error")
        elif len(new_pw) < 6:
            flash("New password must be at least 6 characters.", "error")
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("accounting.dashboard"))
    return render_template("auth/change_password.html")


@auth_bp.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users_list():
    # users is not RLS-protected -> every query here MUST filter by
    # organization_id by hand.
    if request.method == "POST":
        username = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        role = request.form["role"]
        password = request.form["password"]
        exists = User.query.filter_by(
            organization_id=current_user.organization_id, username=username
        ).first()
        limit = user_limit(current_user.organization)
        active_count = User.query.filter_by(organization_id=current_user.organization_id, active=True).count()
        if exists:
            flash("That username already exists.", "error")
        elif limit is not None and active_count >= limit:
            flash(f"Your plan allows up to {limit} active users. "
                  "Upgrade your plan on the Billing page to add more.", "error")
        else:
            u = User(organization_id=current_user.organization_id, username=username,
                      full_name=full_name, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash(f"User {username} created.", "success")
        return redirect(url_for("auth.users_list"))

    all_users = (
        User.query.filter_by(organization_id=current_user.organization_id)
        .order_by(User.role.desc(), User.username)
        .all()
    )
    return render_template("auth/users.html", all_users=all_users)


@auth_bp.route("/users/<uuid:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def user_toggle(user_id):
    u = User.query.filter_by(id=user_id, organization_id=current_user.organization_id).first_or_404()
    if u.id == current_user.id:
        flash("You cannot deactivate your own account.", "error")
    elif not u.active and (user_limit(current_user.organization) is not None
                           and User.query.filter_by(organization_id=current_user.organization_id,
                                                    active=True).count() >= user_limit(current_user.organization)):
        flash("Your plan's user limit is reached. Upgrade on the Billing page to activate more users.", "error")
    else:
        u.active = not u.active
        db.session.commit()
        flash(f"User {u.username} {'activated' if u.active else 'deactivated'}.", "success")
    return redirect(url_for("auth.users_list"))


@auth_bp.route("/users/<uuid:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def user_reset_password(user_id):
    u = User.query.filter_by(id=user_id, organization_id=current_user.organization_id).first_or_404()
    new_pw = request.form["new_password"]
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "error")
    else:
        u.set_password(new_pw)
        db.session.commit()
        flash(f"Password reset for {u.username}.", "success")
    return redirect(url_for("auth.users_list"))
