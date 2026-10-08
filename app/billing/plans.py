"""Subscription plans.

PRICES BELOW ARE PLACEHOLDERS -- confirm them before launch. They are
the single source of truth: both the pricing page and
scripts/create_paystack_plans.py read them, so the price shown to a
customer always matches the amount of the Paystack plan they are
charged on. If you change a price here, create a NEW Paystack plan
(Paystack plan amounts are fixed once created) and point the matching
PAYSTACK_PLAN_* env var at the new plan code.
"""
import os

TRIAL_DAYS = 14

INTERVALS = ("monthly", "annual")

# Paystack's own interval names for the plan-creation API.
PAYSTACK_INTERVAL = {"monthly": "monthly", "annual": "annually"}

# Approximate length of one paid period, used to estimate
# current_period_end between webhooks (the subscription.create event
# later overwrites it with Paystack's exact next_payment_date).
PERIOD_DAYS = {"monthly": 31, "annual": 366}

PLANS = {
    "starter": {
        "name": "Starter",
        "tagline": "For small NGOs and growing businesses",
        "max_users": 3,
        "price_naira": {"monthly": 15_000, "annual": 150_000},
    },
    "organisation": {
        "name": "Organisation",
        "tagline": "For larger teams with more people in the books",
        "max_users": 15,
        "price_naira": {"monthly": 45_000, "annual": 450_000},
    },
}

# Everything below is included in every plan; plans differ by seat count.
FEATURES = [
    "Fund & project accounting with donor reporting",
    "Double-entry journal, trial balance, income statement, balance sheet",
    "Fixed-asset register with automatic depreciation",
    "Nigerian payroll under the Nigeria Tax Act 2025",
    "Invoicing, vendor bills and bank reconciliation",
    "Multi-currency (NGN, USD, EUR, GBP)",
]


def price_kobo(plan_key, interval):
    return PLANS[plan_key]["price_naira"][interval] * 100


def plan_code_env_var(plan_key, interval):
    return f"PAYSTACK_PLAN_{plan_key.upper()}_{interval.upper()}"


def paystack_plan_code(plan_key, interval):
    """The Paystack plan code (PLN_xxx) for this plan/interval, from the
    environment -- None if it hasn't been configured."""
    return os.environ.get(plan_code_env_var(plan_key, interval)) or None


def lookup_by_plan_code(plan_code):
    """Reverse of paystack_plan_code: (plan_key, interval) or (None, None).
    Needed for renewal webhooks, which carry only Paystack's plan code."""
    if not plan_code:
        return None, None
    for key in PLANS:
        for interval in INTERVALS:
            if paystack_plan_code(key, interval) == plan_code:
                return key, interval
    return None, None


def max_users_for(plan_key):
    plan = PLANS.get(plan_key)
    return plan["max_users"] if plan else None
