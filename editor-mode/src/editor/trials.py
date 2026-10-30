"""The free trial, once: per person, company and site.

A trial costs model calls, so it is given once. At every signup the
customer's email, VAT number, tax code and phone, and the owner's email,
are compared with those of every earlier customer; every site added to a
newsroom is compared with the sites of every other newsroom. Whoever comes
back with the same data, or brings a site that already had its trial
elsewhere, gets the newsroom without the trial: it waits for a plan in
/account, and the operator sees why in /admin › Clienti.

What is kept to recognise them is a hash of the normalised value
(``trial_marks``), so it lasts after a newsroom or a customer is deleted
without keeping the data themselves.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import insert, select

from . import models as m
from .auth import digest
from .i18n import N_

KINDS = ("email", "vat", "tax_code", "phone", "domain")
LABELS = {"email": N_("email"), "vat": N_("P.IVA"), "tax_code": N_("codice fiscale"), "phone": N_("telefono"),
          "domain": N_("sito")}
GMAIL = ("gmail.com", "googlemail.com")


class TrialUsed(RuntimeError):
    """A site that already had its trial in another newsroom."""


def norm_email(v: str) -> str:
    v = (v or "").strip().lower()
    if "@" not in v:
        return ""
    local, _, host = v.rpartition("@")
    local = local.split("+", 1)[0]
    if host in GMAIL:  # Gmail ignores dots: a.b@gmail.com is ab@gmail.com
        local, host = local.replace(".", ""), "gmail.com"
    return f"{local}@{host}" if local and host else ""


def norm_phone(v: str) -> str:
    digits = re.sub(r"\D", "", v or "")
    return digits[-9:] if len(digits) >= 7 else ""


def norm_domain(v: str) -> str:
    v = (v or "").strip().lower()
    v = re.sub(r"^[a-z]+://", "", v).split("/", 1)[0].split(":", 1)[0]
    return v[4:] if v.startswith("www.") else v


def norm_code(v: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (v or "").upper())


def keys(customer: dict, owner_email: str = "") -> list[tuple[str, str]]:
    """The (kind, normalised value) pairs a signup is recognised by."""
    out = [("email", norm_email(customer.get("email", ""))), ("email", norm_email(owner_email)),
           ("vat", norm_code(customer.get("vat_number", ""))), ("tax_code", norm_code(customer.get("tax_code", ""))),
           ("phone", norm_phone(customer.get("phone", "")))]
    return list(dict.fromkeys((k, v) for k, v in out if v))


def _hash(kind: str, value: str) -> str:
    return digest(f"{kind}:{value}")


def earlier(conn, pairs: list[tuple[str, str]], newsroom: str, customer_id: int | None = None) -> list[dict]:
    """Earlier signups with any of these values: from the marks, and from
    the customers still in the database (also those from before the marks)."""
    found: list[dict] = []
    t = m.trial_marks
    hashes = {_hash(k, v): (k, v) for k, v in pairs}
    if hashes:
        for r in conn.execute(select(t.c.kind, t.c.value_hash, t.c.newsroom)
                              .where(t.c.value_hash.in_(list(hashes)), t.c.newsroom != newsroom)):
            found.append({"kind": r.kind, "value": hashes[r.value_hash][1], "newsroom": r.newsroom})
    wanted = set(pairs)
    c = m.customers
    rows = conn.execute(select(c.c.id, c.c.email, c.c.vat_number, c.c.tax_code, c.c.phone)).all()
    for r in rows:
        if r.id == customer_id:
            continue
        for k, v in keys(dict(r._mapping)):
            if (k, v) in wanted:
                rooms = conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.customer_id == r.id)).scalars().all()
                rooms = [x for x in rooms if x != newsroom]
                found.append({"kind": k, "value": v, "newsroom": rooms[0] if rooms else None})
    seen, out = set(), []
    for f in found:
        if (f["kind"], f["value"]) not in seen:
            seen.add((f["kind"], f["value"]))
            out.append(f)
    return out


def mark(conn, pairs: list[tuple[str, str]], newsroom: str, customer_id: int | None = None) -> None:
    t = m.trial_marks
    for k, v in pairs:
        h = _hash(k, v)
        if conn.execute(select(t.c.id).where(t.c.value_hash == h, t.c.newsroom == newsroom)).first() is None:
            conn.execute(insert(t).values(kind=k, value_hash=h, newsroom=newsroom, customer_id=customer_id,
                                          created_at=datetime.now(UTC)))


def masked(kind: str, value: str) -> str:
    """Enough to recognise the value in /admin, not the whole of it."""
    if kind == "domain":
        return value
    if kind == "email":
        local, _, host = value.partition("@")
        return f"{local[:2]}…@{host}"
    return f"…{value[-4:]}"


def refused(conn, newsroom: str, customer_id: int | None, matches: list[dict]) -> None:
    """A second trial that was not given, for the operator to see."""
    conn.execute(insert(m.trial_refusals).values(
        newsroom=newsroom, customer_id=customer_id, created_at=datetime.now(UTC),
        reasons=[{"kind": x["kind"], "value": masked(x["kind"], x["value"]), "newsroom": x["newsroom"]}
                 for x in matches]))


def refusals(conn, limit: int = 50) -> list[dict]:
    r, c = m.trial_refusals, m.customers
    rows = conn.execute(select(r.c.created_at, r.c.newsroom, r.c.reasons, c.c.name, c.c.email)
                        .join(c, c.c.id == r.c.customer_id, isouter=True)
                        .order_by(r.c.created_at.desc()).limit(limit)).all()
    return [dict(x._mapping) for x in rows]


def check_domain(admin, newsroom: str, domain: str) -> None:
    """Called when a site is added: on trial, a site that had its trial in
    another newsroom is refused (TrialUsed). Every site is remembered."""
    from . import billing

    d = norm_domain(domain)
    if not d:
        return
    sub = billing.subscription(admin, newsroom)
    other = None
    with admin.engine.begin() as conn:
        if sub is not None and sub["status"] == "trialing":
            t = m.trial_marks
            other = conn.execute(select(t.c.newsroom).where(t.c.value_hash == _hash("domain", d),
                                                            t.c.newsroom != newsroom)).scalar()
            if other is not None:
                refused(conn, newsroom, sub.get("customer_id"), [{"kind": "domain", "value": d, "newsroom": other}])
        if other is None:
            mark(conn, [("domain", d)], newsroom, sub.get("customer_id") if sub else None)
    # After the transaction: raising inside it would roll the recorded refusal
    # back, and the operator would never see why the trial was refused.
    if other is not None:
        raise TrialUsed(d)
