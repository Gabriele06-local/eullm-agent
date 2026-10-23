"""Client for the EuLLM Agent Core API. Editor Mode never talks
to a model provider directly: every model call goes through the Core, which
applies the tenant's token, routing, budgets and audit."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from .i18n import N_
from .progress import step

log = logging.getLogger(__name__)


class CoreError(RuntimeError):
    pass


# Waits before asking a failing provider again, within one call.
RETRY_WAITS = (15, 45)


def transient(e: Exception) -> bool:
    """A failure that usually goes away by itself: the provider overloaded
    (5xx), a rate limit (429), a timeout, the Core not reachable. A refusal
    (spending limit, bad key, invalid answer) is not."""
    msg = str(e)
    if not isinstance(e, CoreError) or msg.startswith("invalid JSON"):
        return False
    return msg.startswith("Core unreachable") or bool(re.match(r"Core returned (5\d\d|429|408)", msg)) \
        or bool(re.search(r"error (5\d\d|429)\b|timed? ?out", msg, re.I))


@dataclass
class ChatResult:
    content: str
    usage: dict | None
    cost: float | None
    model: str


TENANT_HEADER = "X-EuLLM-Tenant"


class CoreClient:
    def __init__(self, base_url: str, token: str, model: str = "default", client: httpx.Client | None = None,
                 guard=None, delegate: bool = False, tenant: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model = model
        self.http = client or httpx.Client(timeout=180)
        # Called before every model call; raises to stop it (tenant quotas).
        self.guard = guard
        # With a delegating Core token (`delegate: true`), each Editor Mode
        # tenant is the Core tenant <token tenant>/<tenant id>: its own
        # usage, audit and limits.
        self.delegate = delegate
        self.tenant = tenant

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if self.tenant:
            h[TENANT_HEADER] = self.tenant
        return h

    def for_tenant(self, db, tenant_id: str) -> "CoreClient":
        """A client for one tenant: its own Core token when it has one, else
        the shared token acting for it when the token delegates; it stops
        when the tenant's monthly model budget is spent."""
        import os

        from sqlalchemy import select

        from . import models as m
        from .quotas import QuotaExceeded, check

        with db.tenant(tenant_id) as s:
            env = s.execute(select(m.tenants.c.core_token_env)).scalar()
        token = os.environ.get(env, "") if env else self.token

        def guard():
            with db.tenant(tenant_id) as s:
                try:
                    check(s, "llm_cost", adding=0)
                except QuotaExceeded as e:
                    raise CoreError(str(e)) from None

        own = bool(env and token)
        return CoreClient(self.base_url, token or self.token, self.model, self.http, guard,
                          delegate=False, tenant=tenant_id if self.delegate and not own else None)

    def set_limits(self, tenant_id: str, limits: dict) -> dict:
        """Set the Core limits of a tenant (PUT /v1/tenants/{tenant}/limits);
        needs the delegating token."""
        try:
            r = self.http.put(f"{self.base_url}/v1/tenants/{tenant_id}/limits",
                              headers={"Authorization": f"Bearer {self.token}"}, json=limits)
        except httpx.HTTPError as e:
            raise CoreError(f"Core unreachable: {e}") from e
        if r.status_code != 200:
            raise CoreError(f"Core returned {r.status_code}: {_error(r)}")
        return r.json()

    def chat(self, messages: list[dict], model: str | None = None) -> ChatResult:
        """One model call. A provider that fails for a moment (5xx, rate
        limit, timeout) is asked again after RETRY_WAITS before giving up."""
        for wait in (*RETRY_WAITS, None):
            try:
                return self._chat(messages, model)
            except CoreError as e:
                if wait is None or not transient(e):
                    raise
                log.warning("model call failed, retrying in %ss: %s", wait, e)
                step(N_("Il modello non ha risposto ({error}): riprovo tra {n} secondi"), error=str(e)[:80], n=wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _chat(self, messages: list[dict], model: str | None = None) -> ChatResult:
        model = model or self.model
        if self.guard is not None:
            self.guard()
        try:
            r = self.http.post(
                f"{self.base_url}/v1/llm/chat",
                headers=self.headers(),
                json={"model": model, "messages": messages},
            )
        except httpx.HTTPError as e:
            raise CoreError(f"Core unreachable: {e}") from e
        if r.status_code != 200:
            raise CoreError(f"Core returned {r.status_code}: {_error(r)}")
        body = r.json()
        # The Core names the model that answered (a fallback, or the real
        # model behind "default"); older Cores do not.
        return ChatResult(body.get("content", ""), body.get("usage"), body.get("cost"), body.get("served_model") or model)

    def chat_json(self, system: str, user: str, validate, model: str | None = None, attempts: int = 2):
        """Ask for a JSON answer and validate it; one retry with the error.

        Returns (value, [ChatResult...]) so the caller can record every call.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        calls = []
        last_error = "no answer"
        for _ in range(attempts):
            res = self.chat(messages, model)
            calls.append(res)
            try:
                return validate(extract_json(res.content)), calls
            except (ValueError, TypeError, KeyError) as e:
                last_error = str(e)
                messages += [
                    {"role": "assistant", "content": res.content},
                    {"role": "user", "content": f"The answer is not valid: {last_error}. Reply with the corrected JSON only."},
                ]
        raise CoreError(f"invalid JSON from model: {last_error}")


def _error(r: httpx.Response) -> str:
    try:
        return r.json().get("error", r.text)
    except ValueError:
        return r.text


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str):
    """The first JSON value in a model answer, with or without a code fence."""
    m = _FENCE.search(text)
    candidate = (m.group(1) if m else text).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = candidate.find(open_), candidate.rfind(close)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON value found")
