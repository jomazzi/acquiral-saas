import uuid

from flask import Flask, g, render_template, redirect, url_for, request, flash
from flask_login import current_user, login_user
from werkzeug.middleware.proxy_fix import ProxyFix

from config import get_config
from app.extensions import db, login_manager, migrate


def set_tenant(organization_id):
    """Explicitly apply the tenant context for the CURRENT transaction.

    Deliberately NOT wired up via an ORM `after_begin`/`before_commit`
    event hook — an earlier attempt at that had unpredictable firing
    timing relative to when queries actually ran, which is worse than
    useless for something security-critical. Called by hand instead:
    once in before_request (for the common case), and again after ANY
    commit that happens mid-request (e.g. signup, where the Organization
    and User rows must be committed before the id even exists, and any
    later query in that same request runs in a fresh transaction that
    has lost the SET LOCAL from before).

    Rationale for the explicitness: prefer a route that fails loudly
    (RLS denies everything if you forget to call this) over one that
    could silently apply the wrong tenant.
    """
    db.session.execute(
        db.text("SET LOCAL app.current_tenant = :tenant"),
        {"tenant": str(organization_id) if organization_id else ""},
    )


def create_app():
    app = Flask(__name__)
    app.config.from_object(get_config())

    if app.config.get("BEHIND_PROXY"):
        # Cloud platforms (Render, Railway, Fly, most PaaS/reverse-proxy
        # setups) terminate HTTPS in front of the app and forward plain
        # HTTP internally, adding X-Forwarded-* headers. Without this,
        # Flask thinks every request is HTTP -- breaking secure cookies,
        # url_for(..., _external=True), and Flask-Login's redirect
        # handling. x_for/x_host default to 1 hop, matching a single
        # reverse proxy in front of the app (the normal PaaS setup).
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    migrate.init_app(app, db)

    from app.models.tenant import User

    @login_manager.user_loader
    def load_user(user_id):
        # Flask-Login's get_id()/user_loader contract passes/returns a
        # string, but our primary keys are UUID objects. db.session.get
        # does NOT coerce the string for you — it just silently returns
        # None on the type mismatch, which looks exactly like "not
        # logged in" rather than an error. Cast explicitly.
        try:
            return db.session.get(User, uuid.UUID(user_id))
        except (ValueError, AttributeError, TypeError):
            return None

    from app.blueprints.auth.routes import auth_bp
    from app.blueprints.accounting.routes import accounting_bp
    from app.blueprints.assets.routes import assets_bp
    from app.blueprints.payroll.routes import payroll_bp
    from app.blueprints.invoicing.routes import invoicing_bp
    from app.blueprints.banking.routes import banking_bp
    from app.blueprints.purchasing.routes import purchasing_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(accounting_bp)
    app.register_blueprint(assets_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(invoicing_bp)
    app.register_blueprint(banking_bp)
    app.register_blueprint(purchasing_bp)

    APP_NAME = "Acquiral"
    APP_TAGLINE = "Fund accounting, fixed assets and Nigerian payroll — in one place."
    COMPANY_NAME = "Admiral Sentinel"
    COMPANY_URL = "https://www.admiralsentinel.com"

    @app.context_processor
    def inject_branding():
        return dict(app_name=APP_NAME, app_tagline=APP_TAGLINE,
                    company_name=COMPANY_NAME, company_url=COMPANY_URL)

    @app.context_processor
    def inject_demo_flag():
        # Drives the read-only banner in base.html -- kept as a template
        # flag rather than templates guessing from the org name/slug.
        is_demo_org = bool(current_user.is_authenticated and current_user.organization.is_demo)
        return dict(is_demo_org=is_demo_org)

    @app.route("/")
    def landing():
        if current_user.is_authenticated:
            return redirect(url_for("accounting.dashboard"))
        return render_template("landing.html")

    @app.route("/demo")
    def try_demo():
        """One-click entry point for a marketing link (e.g. from
        admiralsentinel.com) straight into the shared, read-only demo
        organization -- no credentials to type or remember. Logs the
        visitor in as that organization's admin user; the before_request
        guard below is what actually makes the visit read-only."""
        from app.models.tenant import Organization, User
        demo_org = Organization.query.filter_by(is_demo=True, active=True).first()
        demo_user = User.query.filter_by(organization_id=demo_org.id).order_by(User.username).first() if demo_org else None
        if not demo_user:
            flash("The demo isn't set up yet — check back soon.", "error")
            return redirect(url_for("landing"))
        login_user(demo_user)
        flash("You're in the shared Acquiral demo — explore freely, nothing you change here is saved.", "success")
        return redirect(url_for("accounting.dashboard"))

    @app.before_request
    def apply_tenant_context():
        g.tenant_set = False
        if current_user.is_authenticated:
            set_tenant(current_user.organization_id)
            g.tenant_set = True
            # Read-only enforcement for the shared public demo tenant:
            # block every request that isn't a plain GET, so a visitor
            # can click through the whole app but can never mutate the
            # shared demo data (no reset job needed, and no route has to
            # remember to protect itself individually -- the same
            # deny-by-default philosophy as the RLS setup elsewhere in
            # this app). GET-only means downloads (invoice/payslip PDFs)
            # still work, since those are GET requests.
            if current_user.organization.is_demo and request.method != "GET":
                # Redirect to a fixed internal page rather than
                # request.referrer -- that header is attacker-controlled
                # (an off-site page can still set an arbitrary Referer),
                # so using it as a redirect target would be an open
                # redirect. Not a place to cut that corner.
                flash("This is the shared demo — it's read-only, so that change wasn't saved. "
                      "Sign up to try this for real.", "error")
                return redirect(url_for("accounting.dashboard"))

    return app
