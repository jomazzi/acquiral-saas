"""Minimal Paystack client -- stdlib only, so the image stays
dependency-free (same reasoning as reportlab being pure Python).

Every call raises PaystackError on any failure (network, non-2xx, or
Paystack's own status:false), so routes have one thing to catch."""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

API_BASE = "https://api.paystack.co"

# Paystack sits behind Cloudflare, which rejects the default
# "Python-urllib/x.y" identifier with a 403 (error 1010). Always send our own.
USER_AGENT = "AcquiralBilling/1.0"


class PaystackError(Exception):
    pass


def secret_key():
    return os.environ.get("PAYSTACK_SECRET_KEY") or None


def is_configured():
    return bool(secret_key())


def _request(method, path, payload=None, timeout=20, key=None):
    """`key` overrides the platform key -- used when acting on behalf of a
    tenant with THEIR Paystack account (invoice payments)."""
    key = key or secret_key()
    if not key:
        raise PaystackError("Paystack is not configured (PAYSTACK_SECRET_KEY is unset).")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode(errors="replace")
        except Exception:
            pass
        try:
            msg = json.loads(raw).get("message", "")
        except Exception:
            msg = raw.strip()[:200]
        raise PaystackError(f"Paystack returned HTTP {e.code}: {msg}".strip())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise PaystackError(f"Could not reach Paystack: {e}")
    if not body.get("status"):
        raise PaystackError(body.get("message") or "Paystack reported a failure.")
    return body["data"]


def initialize_transaction(email, amount_minor, currency, plan_code, reference, callback_url, metadata, key=None):
    """Starts a hosted checkout. Passing `plan` makes Paystack create a
    recurring subscription from the successful first charge (and use the
    plan's amount, which is why plan amounts must match plans.py)."""
    payload = {
        "email": email, "amount": amount_minor, "currency": currency,
        "reference": reference, "callback_url": callback_url, "metadata": metadata,
    }
    if plan_code:               # one-off invoice payments have no plan
        payload["plan"] = plan_code
    return _request("POST", "/transaction/initialize", payload, key=key)


def verify_transaction(reference, key=None):
    from urllib.parse import quote
    return _request("GET", f"/transaction/verify/{quote(reference, safe='')}", key=key)


def check_key(key):
    """Cheap authenticated call that proves a secret key is valid."""
    return _request("GET", "/transaction?perPage=1", key=key)


def disable_subscription(subscription_code, email_token):
    return _request("POST", "/subscription/disable",
                    {"code": subscription_code, "token": email_token})


def create_plan(name, amount_minor, interval, currency):
    return _request("POST", "/plan", {
        "name": name, "amount": amount_minor, "interval": interval, "currency": currency,
    })


def valid_webhook_signature(raw_body, signature_header, key=None):
    """Paystack signs the raw request body with HMAC-SHA512 using the
    secret key and sends the hex digest in X-Paystack-Signature. Must be
    computed over the exact bytes received (not re-serialised JSON), and
    compared in constant time."""
    key = key or secret_key()
    if not key or not signature_header:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip().lower())
