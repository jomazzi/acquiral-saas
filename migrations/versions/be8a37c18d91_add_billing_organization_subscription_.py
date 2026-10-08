"""add billing: organization subscription columns and subscription_payments

Revision ID: be8a37c18d91
Revises: 4f6240f6d94e
Create Date: 2026-10-08 14:59:38.956153

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'be8a37c18d91'
down_revision = '4f6240f6d94e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('subscription_payments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=False),
    sa.Column('amount_kobo', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('plan_key', sa.String(length=30), nullable=True),
    sa.Column('plan_interval', sa.String(length=10), nullable=True),
    sa.Column('paid_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reference')
    )
    with op.batch_alter_table('subscription_payments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_subscription_payments_organization_id'), ['organization_id'], unique=False)

    with op.batch_alter_table('organizations', schema=None) as batch_op:
        # Every organisation that exists at migration time (the founding
        # tenant, the demo, any early adopters) is grandfathered as
        # 'comped' -- free, never locked out. The server default exists
        # only so the ALTER succeeds on existing rows; it is dropped
        # straight after, so new organisations get 'trialing' from the
        # application (see signup) rather than silently inheriting it.
        batch_op.add_column(sa.Column('billing_status', sa.String(length=20), nullable=False, server_default='comped'))
        batch_op.add_column(sa.Column('billing_email', sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column('plan_key', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('plan_interval', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('trial_ends_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('current_period_end', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('paystack_customer_code', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('paystack_subscription_code', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('paystack_email_token', sa.String(length=100), nullable=True))
        batch_op.create_index(batch_op.f('ix_organizations_paystack_customer_code'), ['paystack_customer_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_organizations_paystack_subscription_code'), ['paystack_subscription_code'], unique=False)

    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.alter_column('billing_status', server_default=None)


def downgrade():
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_organizations_paystack_subscription_code'))
        batch_op.drop_index(batch_op.f('ix_organizations_paystack_customer_code'))
        batch_op.drop_column('paystack_email_token')
        batch_op.drop_column('paystack_subscription_code')
        batch_op.drop_column('paystack_customer_code')
        batch_op.drop_column('current_period_end')
        batch_op.drop_column('trial_ends_at')
        batch_op.drop_column('plan_interval')
        batch_op.drop_column('plan_key')
        batch_op.drop_column('billing_email')
        batch_op.drop_column('billing_status')

    with op.batch_alter_table('subscription_payments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscription_payments_organization_id'))

    op.drop_table('subscription_payments')
