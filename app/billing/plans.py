"""Subscription plans.

Prices are the single source of truth: both the pricing page and
scripts/create_paystack_plans.py read them, so the price shown to a
customer always matches the amount of the Paystack plan they are
charged on. If you change a price here, create a NEW Paystack plan
(Paystack plan amounts are fixed once created) and point the matching
PAYSTACK_PLAN_* env var at the new plan code.

There is one Paystack plan per plan x interval x currency (8 in all).
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

CURRENCIES = ("NGN", "USD")
DEFAULT_CURRENCY = "NGN"
CURRENCY_SYMBOL = {"NGN": "\u20a6", "USD": "$"}

PLANS = {
    "starter": {
        "name": "Starter",
        "tagline": "For small NGOs and growing businesses",
        "max_users": 3,
        # Whole units of each currency (naira / dollars), per period.
        "price": {
            "NGN": {"monthly": 45_000, "annual": 450_000},
            "USD": {"monthly": 21, "annual": 252},
        },
    },
    "organisation": {
        "name": "Organisation",
        "tagline": "For larger teams with more people in the books",
        "max_users": 15,
        "price": {
            "NGN": {"monthly": 69_000, "annual": 828_000},
            "USD": {"monthly": 51, "annual": 612},
        },
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


def price(plan_key, interval, currency):
    """Price in whole currency units (naira / dollars)."""
    return PLANS[plan_key]["price"][currency][interval]


def price_minor(plan_key, interval, currency):
    """Price in the currency's minor unit (kobo / cents), which is what
    Paystack's API takes."""
    return price(plan_key, interval, currency) * 100


def annual_saving(plan_key, currency):
    """What paying annually saves versus 12 monthly payments (0 if it
    doesn't). Computed, never hard-coded, so the pricing copy can't drift
    from the actual prices."""
    p = PLANS[plan_key]["price"][currency]
    return max(0, p["monthly"] * 12 - p["annual"])


def format_money(amount, currency, minor=False):
    """'\u20a645,000' / '$21'. `minor=True` takes kobo/cents."""
    if minor:
        amount = amount / 100
    sym = CURRENCY_SYMBOL.get(currency, currency + " ")
    return f"{sym}{amount:,.2f}" if (minor and amount != int(amount)) else f"{sym}{int(amount):,}"


def plan_code_env_var(plan_key, interval, currency):
    return f"PAYSTACK_PLAN_{plan_key.upper()}_{interval.upper()}_{currency.upper()}"


def paystack_plan_code(plan_key, interval, currency):
    """The Paystack plan code (PLN_xxx) for this plan/interval/currency,
    from the environment -- None if it hasn't been configured."""
    return os.environ.get(plan_code_env_var(plan_key, interval, currency)) or None


def lookup_by_plan_code(plan_code):
    """Reverse of paystack_plan_code: (plan_key, interval, currency) or
    (None, None, None). Needed for renewal webhooks, which carry only
    Paystack's plan code."""
    if not plan_code:
        return None, None, None
    for key in PLANS:
        for interval in INTERVALS:
            for currency in CURRENCIES:
                if paystack_plan_code(key, interval, currency) == plan_code:
                    return key, interval, currency
    return None, None, None


def max_users_for(plan_key):
    plan = PLANS.get(plan_key)
    return plan["max_users"] if plan else None
