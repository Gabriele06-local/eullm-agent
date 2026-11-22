"""Payment gateways behind one small interface, so that another one
(Stripe, a bank's) can take Mollie's place without touching billing.py.

A gateway creates its customer, opens the first payment (which also
authorises the renewals), reads a payment back, starts or cancels the
recurring subscription, and refunds a payment. Its notices carry only an id: billing.py always
reads the payment from the gateway before acting, so a forged notice can do
nothing.

Mollie API v2 (https://docs.mollie.com/reference): customers, payments with
sequenceType "first", customer subscriptions. The key comes from
EDITOR_MOLLIE_API_KEY (test_... keys use Mollie's test mode).
"""

from __future__ import annotations

from datetime import date

import httpx

MOLLIE_API = "https://api.mollie.com/v2"


class GatewayError(RuntimeError):
    pass


def _amount(cents: int, currency: str) -> dict:
    value = int(cents)
    sign = "-" if value < 0 else ""
    whole, rest = divmod(abs(value), 100)
    return {"currency": currency, "value": f"{sign}{whole}.{rest:02d}"}


def _cents(amount: dict | None) -> int:
    if not amount:
        return 0
    raw = str(amount.get("value", "0")).strip()
    sign = -1 if raw.startswith("-") else 1
    whole, _, frac = raw.lstrip("+-").partition(".")
    return sign * (int(whole or 0) * 100 + int((frac + "00")[:2]))


class Mollie:
    name = "mollie"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key.startswith(("live_", "test_")):
            raise GatewayError("the Mollie key starts with live_ or test_")
        self.test = api_key.startswith("test_")
        self.http = client or httpx.Client(timeout=20)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _call(self, method: str, path: str, body: dict | None = None) -> dict | None:
        r = self.http.request(method, f"{MOLLIE_API}{path}", json=body, headers=self.headers)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.text
            except ValueError:
                detail = r.text
            raise GatewayError(f"Mollie {r.status_code}: {detail[:300]}")
        return r.json() if r.content else {}

    def create_customer(self, name: str, email: str, metadata: dict) -> str:
        return self._call("POST", "/customers", {"name": name, "email": email, "metadata": metadata})["id"]

    def first_payment(self, customer_id: str, amount_cents: int, currency: str, description: str, redirect_url: str,
                      webhook_url: str, metadata: dict) -> tuple[str, str]:
        """The payment that starts a plan; returns (id, checkout address)."""
        p = self._call("POST", "/payments", {
            "amount": _amount(amount_cents, currency), "description": description[:255], "customerId": customer_id,
            "sequenceType": "first", "redirectUrl": redirect_url, "webhookUrl": webhook_url, "metadata": metadata})
        return p["id"], p["_links"]["checkout"]["href"]

    def one_off_payment(self, customer_id: str, amount_cents: int, currency: str, description: str,
                        redirect_url: str, webhook_url: str, metadata: dict) -> tuple[str, str]:
        """A single payment (a pack of credits); returns (id, checkout address)."""
        p = self._call("POST", "/payments", {
            "amount": _amount(amount_cents, currency), "description": description[:255], "customerId": customer_id,
            "sequenceType": "oneoff", "redirectUrl": redirect_url, "webhookUrl": webhook_url, "metadata": metadata})
        return p["id"], p["_links"]["checkout"]["href"]

    def payment(self, payment_id: str) -> dict | None:
        if not payment_id.startswith("tr_"):
            return None
        p = self._call("GET", f"/payments/{payment_id}")
        if p is None:
            return None
        return {"id": p["id"], "status": p["status"], "amount_cents": _cents(p.get("amount")),
                "currency": (p.get("amount") or {}).get("currency", "EUR"), "metadata": p.get("metadata") or {},
                "mandate_id": p.get("mandateId"), "subscription_id": p.get("subscriptionId"),
                "customer_id": p.get("customerId"), "refunded_cents": _cents(p.get("amountRefunded")),
                "remaining_cents": _cents(p.get("amountRemaining")) if p.get("amountRemaining") else None}

    def refund(self, payment_id: str, amount_cents: int, currency: str, description: str) -> tuple[str, str]:
        """Give back part or all of a paid payment; returns (refund id, status)."""
        r = self._call("POST", f"/payments/{payment_id}/refunds",
                       {"amount": _amount(amount_cents, currency), "description": description[:140]})
        if r is None:
            raise GatewayError("Mollie 404: payment not found")
        return r["id"], r.get("status", "queued")

    def refund_status(self, payment_id: str, refund_id: str) -> str | None:
        """The refund's status now (queued, pending, processing, refunded,
        failed, canceled), None when Mollie does not know it."""
        r = self._call("GET", f"/payments/{payment_id}/refunds/{refund_id}")
        return None if r is None else r.get("status")

    def create_subscription(self, customer_id: str, amount_cents: int, currency: str, interval: str, start: date,
                            description: str, webhook_url: str, mandate_id: str | None, metadata: dict) -> str:
        body = {"amount": _amount(amount_cents, currency), "interval": "12 months" if interval == "year" else "1 month",
                "startDate": start.isoformat(), "description": description[:255], "webhookUrl": webhook_url,
                "metadata": metadata}
        if mandate_id:
            body["mandateId"] = mandate_id
        return self._call("POST", f"/customers/{customer_id}/subscriptions", body)["id"]

    def update_subscription(self, customer_id: str, subscription_id: str, amount_cents: int, currency: str) -> None:
        """The amount of the next renewals (a language added or removed)."""
        self._call("PATCH", f"/customers/{customer_id}/subscriptions/{subscription_id}",
                   {"amount": _amount(amount_cents, currency)})

    def cancel_subscription(self, customer_id: str, subscription_id: str) -> None:
        """Stop the renewals. Already stopped (cancelled earlier, for example
        by a refund, or completed) counts as done: Mollie refuses a second
        cancel, and the console would say the renewals are still on."""
        try:
            self._call("DELETE", f"/customers/{customer_id}/subscriptions/{subscription_id}")
        except GatewayError:
            sub = self._call("GET", f"/customers/{customer_id}/subscriptions/{subscription_id}")
            if sub is not None and sub.get("status") not in ("canceled", "completed"):
                raise


def from_settings(settings) -> Mollie | None:
    key = getattr(settings, "mollie_api_key", "")
    return Mollie(key) if key else None
