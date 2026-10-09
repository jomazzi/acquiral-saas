"""One-off: create the Acquiral subscription plans in your Paystack account.

Usage (use the TEST secret key first, then repeat once with the LIVE key):

    PAYSTACK_SECRET_KEY=sk_test_xxx PYTHONPATH=. python scripts/create_paystack_plans.py

Prints one PAYSTACK_PLAN_* line per plan/interval/currency (8 in all). Add those to the web
service's environment variables (Render -> Environment) together with
PAYSTACK_SECRET_KEY. Plans come from app/billing/plans.py, so prices
here always match what the pricing page shows.

Paystack plan amounts can't be edited after creation. To change a price:
edit plans.py, run this again (it skips plans whose env var is already
set, so unset the one you're replacing), and update the env var.

USD plans need USD enabled on your Paystack account. If it isn't, those
lines print FAILED and the NGN plans are still created; re-run once USD
is enabled (already-created plans are skipped only if their env var is
set, so unset or remove the ones you've already added to avoid duplicates).
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
            for currency in plans.CURRENCIES:
                var = plans.plan_code_env_var(plan_key, interval, currency)
                if plans.paystack_plan_code(plan_key, interval, currency):
                    print(f"# {var} is already set in this shell -- skipping")
                    continue
                try:
                    created = paystack.create_plan(
                        name=f"Acquiral {plan['name']} ({interval}, {currency})",
                        amount_minor=plans.price_minor(plan_key, interval, currency),
                        interval=plans.PAYSTACK_INTERVAL[interval],
                        currency=currency,
                    )
                except paystack.PaystackError as e:
                    # Most likely: USD isn't enabled on the Paystack account yet.
                    print(f"# {var}: FAILED -- {e}")
                    continue
                print(f"{var}={created['plan_code']}")


if __name__ == "__main__":
    main()
