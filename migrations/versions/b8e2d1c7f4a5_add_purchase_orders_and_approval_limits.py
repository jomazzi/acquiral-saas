"""add purchase orders, po lines, audit trail, and user approval limits

Revision ID: b8e2d1c7f4a5
Revises: a7e1d4c9b2f6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b8e2d1c7f4a5"
down_revision = "a7e1d4c9b2f6"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add approval_limit to users table and widen role column
    op.add_column("users", sa.Column("approval_limit", sa.Float(), nullable=False, server_default="0.0"))
    op.alter_column("users", "role", existing_type=sa.String(20), type_=sa.String(30), existing_nullable=True)

    # 2. Create purchase_orders table
    op.create_table(
        "purchase_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("po_number", sa.String(30), nullable=False),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("vendors.id"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("po_date", sa.Date(), nullable=False),
        sa.Column("delivery_date", sa.Date()),
        sa.Column("reference", sa.String(100)),
        sa.Column("notes", sa.Text()),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("preparer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("bill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bills.id"), nullable=True),
        sa.UniqueConstraint("organization_id", "po_number", name="uq_po_org_number"),
    )

    # 3. Create po_lines table
    op.create_table(
        "po_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("po_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("description", sa.String(300)),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0.0"),
    )

    # 4. Create po_audit_trails table
    op.create_table(
        "po_audit_trails",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("po_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("details", sa.Text()),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("current_hash", sa.String(64), nullable=False),
    )


def downgrade():
    op.drop_table("po_audit_trails")
    op.drop_table("po_lines")
    op.drop_table("purchase_orders")
    op.drop_column("users", "approval_limit")
