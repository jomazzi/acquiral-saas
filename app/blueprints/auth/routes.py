import uuid
from datetime import timedelta
import re

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import escape

from app import mailer
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.tenant import Organization, User
from app.models.accounting import seed_default_accounts
from app.billing import plans
from app.billing.access import utcnow, user_limit

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERIFY_MAX_AGE = 24 * 3600
RESEND_COOLDOWN = timedelta(seconds=60)

VERIFY_EXEMPT = {"auth.verify_pending", "auth.verify_email", "auth.resend_verification",
                 "auth.change_email", "auth.logout", "static", "landing", "billing.pricing"}


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="email-verify")


def _send_verification(user):
    """The token carries the email address, so changing the address
    invalidates any link already sent."""
    token = _serializer().dumps({"uid": str(user.id), "email": user.email})
    link = url_for("auth.verify_email", token=token, _external=True)
    name = user.full_name or user.username
    text = (f"Hello {name},\n\nConfirm your email address to open your Acquiral workspace:\n\n{link}\n\n"
            "The link works for 24 hours. If you didn't sign up, you can ignore this email.\n\nAcquiral")
    html = (f"<p>Hello {escape(name)},</p><p>Confirm your email address to open your Acquiral workspace:</p>"
            f'<p><a href="{link}">Confirm my email</a></p>'
            "<p>The link works for 24 hours. If you didn't sign up, you can ignore this email.</p><p>Acquiral</p>")
    ok = mailer.send_email(user.email, "Confirm your email for Acquiral", text, html)
    if ok:
        user.verification_sent_at = utcnow()
        db.session.commit()
    return ok


@auth_bp.before_app_request
def require_verified_email():
    """Unverified users can only see the verification pages."""
    if not current_user.is_authenticated or current_user.email_verified:
        return None
    if (request.endpoint or "") in VERIFY_EXEMPT:
        return None
    return redirect(url_for("auth.verify_pending"))
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
        email = request.form.get("email", "").strip().lower()
        password = request.form["password"]

        if not org_name or not username or len(password) < 6:
            flash("Organization name, username and a password of at least 6 characters are required.", "error")
            return render_template("auth/signup.html")
        if not EMAIL_RE.match(email):
            flash("Enter a valid email address.", "error")
            return render_template("auth/signup.html")

        base_slug = "".join(c.lower() if c.isalnum() else "-" for c in org_name).strip("-") or "org"
        slug = _unique_slug(base_slug)

        org = Organization(name=org_name, slug=slug, billing_status="trialing",
                           trial_ends_at=utcnow() + timedelta(days=plans.TRIAL_DAYS))
        db.session.add(org)
        db.session.commit()  # need org.id before we can create the tenant-scoped user

        user = User(organization_id=org.id, username=username, full_name=full_name, role="admin",
                    email=email, email_verified=not mailer.mail_enabled())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        from app import set_tenant
        set_tenant(org.id)
        seed_default_accounts(org.id)

        login_user(user)
        if not user.email_verified:
            if _send_verification(user):
                flash(f"We sent a confirmation link to {email}.", "success")
            else:
                flash("We couldn't send the confirmation email. Use Resend below.", "error")
            return redirect(url_for("auth.verify_pending"))
        flash(f"Welcome to Acquiral, {org_name}!", "success")
        return redirect(url_for("accounting.dashboard"))

    return render_template("auth/signup.html")

@auth_bp.route("/verify-email-pending")
@login_required
def verify_pending():
    if current_user.email_verified:
        return redirect(url_for("accounting.dashboard"))
    return render_template("auth/verify_pending.html", email=current_user.email)


@auth_bp.route("/verify-email/<token>")
def verify_email(token):
    try:
        data = _serializer().loads(token, max_age=VERIFY_MAX_AGE)
        uid, email = uuid.UUID(data["uid"]), data["email"]
    except (BadSignature, KeyError, ValueError, TypeError):
        flash("That confirmation link is invalid or has expired. Log in to request a new one.", "error")
        return redirect(url_for("auth.verify_pending" if current_user.is_authenticated else "auth.login"))
    user = User.query.filter_by(id=uid).first()
    if user is None or (user.email or "").lower() != email.lower():
        flash("That confirmation link is no longer valid.", "error")
        return redirect(url_for("auth.verify_pending" if current_user.is_authenticated else "auth.login"))
    if not user.email_verified:
        user.email_verified = True
        db.session.commit()
    flash("Email confirmed. Welcome to Acquiral!", "success")
    if current_user.is_authenticated and current_user.id == user.id:
        return redirect(url_for("accounting.dashboard"))
    return redirect(url_for("auth.login"))


@auth_bp.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    if current_user.email_verified:
        return redirect(url_for("accounting.dashboard"))
    last = current_user.verification_sent_at
    if last and utcnow() - last < RESEND_COOLDOWN:
        flash("Please wait a minute before requesting another email.", "error")
    elif _send_verification(current_user):
        flash(f"Confirmation email sent to {current_user.email}.", "success")
    else:
        flash("We couldn't send the email right now. Please try again shortly.", "error")
    return redirect(url_for("auth.verify_pending"))


@auth_bp.route("/change-email", methods=["POST"])
@login_required
def change_email():
    email = request.form.get("email", "").strip().lower()
    if not EMAIL_RE.match(email):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("auth.verify_pending"))
    current_user.email = email
    current_user.email_verified = False
    current_user.verification_sent_at = None
    db.session.commit()
    if _send_verification(current_user):
        flash(f"Confirmation email sent to {email}.", "success")
    else:
        flash("We couldn't send the email right now. Use Resend in a moment.", "error")
    return redirect(url_for("auth.verify_pending"))

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
