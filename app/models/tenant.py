import uuid
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db


class Organization(db.Model):
    """A tenant. Every customer of Acquiral gets one; everything else
    (accounts, employees, journal entries, ...) belongs to exactly one
    Organization via organization_id."""
    __tablename__ = "organizations"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    active = db.Column(db.Boolean, default=True)
    # A single, shared public demo tenant (see scripts/seed_demo.py) is
    # flagged here rather than detected by name/slug -- app/__init__.py's
    # before_request blocks every non-GET request for a logged-in user
    # whose organization has this set, so a visitor can explore freely
    # but can never mutate the shared demo data (no reset job needed).
    is_demo = db.Column(db.Boolean, default=False, nullable=False)


class User(UserMixin, db.Model):
    """Deliberately NOT row-secured (see scripts/setup_rls.sql for why:
    logging in requires looking up a user to discover their tenant, which
    would be circular if that lookup itself required tenant context to
    already be set). Isolation for this table is enforced explicitly in
    application code instead — every query against User MUST filter by
    organization_id by hand. Grep for `User.query` before adding a route."""
    __tablename__ = "users"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organizations.id"), nullable=False, index=True)
    username = db.Column(db.String(80), nullable=False)
    full_name = db.Column(db.String(150))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="user")  # 'admin' or 'user'
    active = db.Column(db.Boolean, default=True)

    organization = db.relationship("Organization")

    # Username unique WITHIN an org only — two tenants can both have "user1".
    __table_args__ = (db.UniqueConstraint("organization_id", "username", name="uq_user_org_username"),)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def get_id(self):
        return str(self.id)


class TenantScopedMixin:
    """Every table using this gets organization_id automatically, and MUST
    also get a matching RLS policy in scripts/setup_rls.sql — the mixin
    alone does not enforce anything; it just adds the column consistently."""

    @declared_attr
    def organization_id(cls):
        return db.Column(UUID(as_uuid=True), db.ForeignKey("organizations.id"), nullable=False, index=True)
