"""One-off: create the Acquiral subscription plans in your Paystack account.

Usage (use the TEST secret key first, then repeat once with the LIVE key):

    PAYSTACK_SECRET_KEY=sk_test_xxx PYTHONPATH=. python scripts/create_paystack_plans.py

Prints one PAYSTACK_PLAN_* line per plan/interval. Add those to the web
service's environment variables (Render -> Environment) together with
PAYSTACK_SECRET_KEY. Plans come from app/billing/plans.py, so prices
here always match what the pricing page shows.

Paystack plan amounts can't be edited after creation. To change a price:
edit plans.py, run this again (it skips plans whose env var is already
set, so unset the one you're replacing), and update the env var.
"""
import sys

from app.billing import paystack, plans


def main():
    if not paystack.is_configured():
        sys.exit("Set PAYSTACK_SECRET_KEY first (sk_test_... or sk_live_...).")

    key_kind = "LIVE" if paystack.secret_key().startswith("sk_live") else "TEST"
    print(f"# Creating plans with a {key_kind} key\n")

    for plan_key, plan in plans.PLANS.items():
        for interval in plans.INTERVALS:
            var = plans.plan_code_env_var(plan_key, interval)
            if plans.paystack_plan_code(plan_key, interval):
                print(f"# {var} is already set in this shell -- skipping")
                continue
            created = paystack.create_plan(
                name=f"Acquiral {plan['name']} ({interval})",
                amount_kobo=plans.price_kobo(plan_key, interval),
                interval=plans.PAYSTACK_INTERVAL[interval],
            )
            print(f"{var}={created['plan_code']}")


if __name__ == "__main__":
    main()
