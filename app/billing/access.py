"""Decides what a tenant is allowed to do, from its billing columns.

The state is DERIVED from dates every time rather than trusted from the
stored status string alone, so it self-heals: if a renewal webhook is
missed, access doesn't silently stay open forever, and if one is
delayed, the grace window covers it.
"""
from datetime import datetime, timedelta, timezone

from app.billing import paystack

# How long past current_period_end an active/past-due subscription keeps
# full access, to absorb Paystack's retry of a failed renewal card charge
# and any webhook delay.
GRACE = timedelta(days=5)

# States where the organisation can read AND write.
WRITABLE_STATES = {"comped", "trialing", "active", "canceling", "past_due"}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def billing_enforced():
    """Enforcement only applies once Paystack is configured. Without
    that there is no way for a customer to pay, so locking them out would
    be a trap -- a deploy with no keys behaves exactly as before."""
    return paystack.is_configured()


def access_state(org, now=None):
    """One of: comped, trialing, active, canceling, past_due, expired."""
    now = now or utcnow()
    if org.is_demo or org.billing_status == "comped":
        return "comped"
    if org.billing_status == "trialing":
        return "trialing" if org.trial_ends_at and now <= org.trial_ends_at else "expired"

    end = org.current_period_end
    if org.billing_status in ("active", "canceled", "past_due"):
        if end and now <= end:
            if org.billing_status == "canceled":
                return "canceling"      # cancelled, but paid through period end
            return "active" if org.billing_status == "active" else "past_due"
        if end and now <= end + GRACE and org.billing_status != "canceled":
            return "past_due"
    return "expired"


def is_writable(org):
    return (not billing_enforced()) or access_state(org) in WRITABLE_STATES


def user_limit(org):
    """Max ACTIVE users this org may have, or None for unlimited.
    Trials get the largest plan's allowance so people can evaluate the
    product properly; comped orgs (founding tenant, demo) and any
    deploy without Paystack configured are unlimited."""
    from app.billing import plans
    if not billing_enforced():
        return None
    state = access_state(org)
    if state == "comped":
        return None
    if state == "trialing":
        return max(p["max_users"] for p in plans.PLANS.values())
    return plans.max_users_for(org.plan_key)


def trial_days_left(org, now=None):
    now = now or utcnow()
    if not org.trial_ends_at:
        return 0
    return max(0, (org.trial_ends_at - now).days + (1 if org.trial_ends_at > now else 0))
