"""Customers, resellers, plans and subscriptions.

The public site sends a new customer through the operator API (signup):
the customer's billing data, a newsroom on the trial plan, its owner
invited by email. When the trial ends without a payment the newsroom goes to proposals only
for PROPOSALS_ONLY_DAYS (a couple of proposals a day, no drafts, no
publishing: the owner still sees what the newsroom finds), then it is
put on hold: its scheduled work stops, its data stay, and its owner can
still sign in to choose a plan and pay (/account). A payment (Mollie, a
bank transfer recorded by the operator, or a reseller's word) starts or
extends the paid period; when that period ends without a new payment the
newsroom goes on hold again after GRACE_DAYS.

A reseller sells newsrooms to its own customers with an operator key
bound to it: it sees and changes only those, and bills them itself
(provider "reseller": no trial expiry unless it sets a period).

Everything here goes through the owner connection (`admin`).
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, insert, select, update

from . import accounts, credits, i18n, newsrooms
from . import models as m
from .i18n import N_, t

log = logging.getLogger(__name__)

TRIAL_PLAN = "trial"
GRACE_DAYS = 3  # after a paid period ends, before the hold
REMIND_DAYS = 2  # before the trial ends
PROPOSALS_ONLY_DAYS = 30  # after the trial or a paid period, before the hold
INTERVALS = ("month", "year")
PROVIDERS = ("manual", "mollie", "reseller")
# VAT on what the gateway charges (Francesco 2026-11-10): Italian VAT on the
# list price, none for a business with a VAT number in another EU country
# (reverse charge, art. 7-ter DPR 633/72: the customer accounts for it).
VAT_PERCENT = 22
EU = frozenset("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())
STATUS_LABELS = {
    "trialing": N_("In prova"),
    "active": N_("Attivo"),
    "past_due": N_("Pagamento non riuscito"),
    "canceled": N_("Disdetto"),
    "expired": N_("Scaduto"),
}
CUSTOMER_FIELDS = ("kind", "name", "email", "phone", "vat_number", "tax_code", "address", "postal_code", "city",
                   "province", "country", "sdi_code", "pec")
PLAN_LIMITS = ("max_sites", "max_active_sources", "max_drafts_month", "max_llm_cost_month",
               "max_languages", "credits_month", "max_users")


class BillingError(ValueError):
    pass


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def add_interval(when: datetime, interval: str) -> datetime:
    """One month (or year) later; the 31st becomes the month's last day."""
    months = 12 if interval == "year" else 1
    y, mo = divmod(when.month - 1 + months, 12)
    year, month = when.year + y, mo + 1
    return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))



def vat_rate(customer: dict | None) -> int:
    """The VAT percent on this customer's online payments."""
    c = customer or {}
    country = (c.get("country") or "IT").upper()
    if country != "IT" and country in EU and c.get("vat_number"):
        return 0
    return VAT_PERCENT


def with_vat(net_cents: int, customer: dict | None) -> tuple[int, int]:
    """(what the customer pays, of which VAT) for a price before VAT."""
    vat = round(net_cents * vat_rate(customer) / 100)
    return net_cents + vat, vat


def vat_in(gross_cents: int, customer: dict | None) -> int:
    """The VAT inside an amount already charged (a renewal Mollie made)."""
    r = vat_rate(customer)
    return gross_cents - round(gross_cents * 100 / (100 + r)) if r else 0

def money(cents: int | None, currency: str = "EUR") -> str:
    if cents is None:
        return "–"
    value = int(cents)
    sign = "-" if value < 0 else ""
    whole, rest = divmod(abs(value), 100)
    symbol = "€" if currency == "EUR" else currency
    return f"{sign}{whole:,}".replace(",", ".") + (f",{rest:02d}" if rest else "") + f" {symbol}"


# --- plans ---------------------------------------------------------------------------

def plans(admin, public_only: bool = False) -> list[dict]:
    p = m.plans
    # Cheapest first (Francesco 2026-11-08), plans without a price last.
    q = select(p).order_by(p.c.price_month_cents.is_(None), p.c.price_month_cents, p.c.sort, p.c.code)
    if public_only:
        q = q.where(p.c.public)
    with accounts._read(admin) as conn:
        return [dict(r._mapping) for r in conn.execute(q).all()]


def get_plan(conn, code: str):
    plan = conn.execute(select(m.plans).where(m.plans.c.code == code)).first()
    if plan is None:
        raise BillingError(t("piano sconosciuto: {code}", code=code))
    return plan


def price(plan, interval: str) -> int | None:
    return plan.price_year_cents if interval == "year" else plan.price_month_cents


def save_plan(admin, code: str, fields: dict) -> None:
    """Change a plan of the catalogue (name, prices, limits, trial days,
    shown on the public site or not). Newsrooms already on it keep their
    limits until their plan is applied again (the next payment)."""
    allowed = {"name", "description", "price_month_cents", "price_year_cents", "trial_days", "max_sites",
               "max_languages", "max_active_sources", "max_drafts_month", "max_llm_cost_month",
               "public", "sort", "autopilot", "credits_month", "evening", "max_users"}
    values = {k: v for k, v in fields.items() if k in allowed}
    for k, v in values.items():
        if k not in ("name", "description", "public", "autopilot", "evening") and v is not None and (isinstance(v, bool) or v < 0):
            raise BillingError(t("{field}: un numero di zero o più", field=k))
    if "name" in values and not str(values["name"]).strip():
        raise BillingError(t("il piano ha bisogno di un nome"))
    with admin.engine.begin() as conn:
        if not conn.execute(update(m.plans).where(m.plans.c.code == code).values(**values)).rowcount:
            raise BillingError(t("piano sconosciuto: {code}", code=code))


def plan_code_of(name: str) -> str:
    """A plan's code from its name: "Pro Plus" → "pro-plus"."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:40]


def create_plan(admin, code: str, name: str) -> str:
    """A new plan in the catalogue, hidden from the public site until its
    prices and limits are set and it is made public."""
    typed = (code or "").strip().lower()
    code = typed or plan_code_of(name)
    if not re.fullmatch(r"[a-z0-9-]{1,40}", code):
        raise BillingError(t("il codice è fatto di lettere minuscole, numeri e trattini (es. pro-plus)"))
    name = (name or "").strip()[:100]
    if not name:
        raise BillingError(t("il piano ha bisogno di un nome"))
    with admin.engine.begin() as conn:
        taken = set(conn.execute(select(m.plans.c.code)).scalars())
        if code in taken and typed:
            owner = conn.execute(select(m.plans.c.name).where(m.plans.c.code == code)).scalar()
            raise BillingError(t("il codice {code} è già del piano {name}", code=code, name=owner))
        # A plan renamed keeps its code: a new plan with the old name gets the next free one.
        base, n = code[:36], 2
        while code in taken:
            code, n = f"{base}-{n}", n + 1
        last = conn.execute(select(func.max(m.plans.c.sort))).scalar() or 0
        conn.execute(insert(m.plans).values(code=code, name=name, description="", currency="EUR", trial_days=0,
                                            public=False, sort=last + 10, autopilot=True, evening=False))
    return code


def delete_plan(admin, code: str) -> str:
    """Remove a plan nobody is on. The trial plan stays: new sign-ups start on it."""
    if code == TRIAL_PLAN:
        raise BillingError(t("il piano di prova serve a chi si iscrive: non si elimina"))
    with admin.engine.begin() as conn:
        plan = conn.execute(select(m.plans).where(m.plans.c.code == code)).first()
        if plan is None:
            raise LookupError(code)
        # A newsroom on the plan has it both in its subscription and in its own row: count it once.
        rooms = set(conn.execute(select(m.subscriptions.c.tenant_id).where(
            (m.subscriptions.c.plan_code == code) | (m.subscriptions.c.desired_plan == code))).scalars())
        rooms |= set(conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.plan == code)).scalars())
        if rooms:
            names = conn.execute(select(m.tenants.c.name, m.tenants.c.tenant_id).where(
                m.tenants.c.tenant_id.in_(rooms)).order_by(m.tenants.c.name)).all()
            raise BillingError(t("{name} è ancora il piano di: {rooms}. Cambialo in Redazioni › Abbonamento, oppure "
                                 "toglilo dal sito pubblico", name=plan.name,
                                 rooms=", ".join(r.name or r.tenant_id for r in names) or ", ".join(sorted(rooms))))
        conn.execute(delete(m.plans).where(m.plans.c.code == code))
    return plan.name


def apply_plan(admin, tenant_id: str, plan, core=None) -> str | None:
    """The plan's limits on the newsroom (and its model budget in the Core)."""
    warning = newsrooms.update_newsroom(admin, tenant_id, {
        "plan": plan.code, "limits": {k: getattr(plan, k) for k in PLAN_LIMITS}}, core)["warning"]
    with admin.engine.begin() as conn:
        conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id)
                     .values(autopilot=plan.autopilot is not False, evening=bool(getattr(plan, "evening", False))))
    return warning


# --- customers -----------------------------------------------------------------------

def clean_customer(spec: dict, partial: bool = False) -> dict:
    """The billing data, checked: what an Italian invoice needs (VAT number
    or tax code, address, SDI code or PEC) and the same for other countries."""
    out = {}
    for k in CUSTOMER_FIELDS:
        if k in spec and spec[k] is not None:
            out[k] = str(spec[k]).strip()[:200]
    if not partial:
        for k in ("name", "email"):
            if not out.get(k):
                raise BillingError(t("manca il campo {field}", field=k))
    if "kind" in out and out["kind"] not in ("company", "person"):
        raise BillingError(t("kind è company o person"))
    if "name" in out and not out["name"]:
        raise BillingError(t("serve un nome o una ragione sociale"))
    if "email" in out:
        out["email"] = accounts.normalise_email(out["email"])
    if out.get("pec"):
        try:
            out["pec"] = accounts.normalise_email(out["pec"])
        except accounts.AccountError as e:
            raise BillingError(t("PEC non valida")) from e
    if "country" in out:
        out["country"] = out["country"].upper() or "IT"
        if not re.fullmatch(r"[A-Z]{2}", out["country"]):
            raise BillingError(t("il paese è un codice di due lettere (IT, DE, FR...)"))
    country = out.get("country", "IT")
    if out.get("vat_number"):
        vat = re.sub(r"[\s.-]", "", out["vat_number"]).upper()
        if vat.startswith(country) and country != "IT" or vat.startswith("IT"):
            vat = vat[2:]
        if country == "IT" and not re.fullmatch(r"\d{11}", vat):
            raise BillingError(t("la partita IVA italiana ha 11 cifre"))
        out["vat_number"] = vat
    if out.get("tax_code"):
        out["tax_code"] = re.sub(r"\s", "", out["tax_code"]).upper()
        if country == "IT" and not re.fullmatch(r"[A-Z0-9]{11}|[A-Z0-9]{16}", out["tax_code"]):
            raise BillingError(t("il codice fiscale ha 16 caratteri (11 cifre per un'azienda)"))
    if out.get("sdi_code"):
        out["sdi_code"] = out["sdi_code"].upper()
        if not re.fullmatch(r"[A-Z0-9]{6,7}", out["sdi_code"]):
            raise BillingError(t("il codice destinatario SDI ha 7 caratteri (6 per la pubblica amministrazione)"))
    if out.get("province"):
        out["province"] = out["province"].upper()
    return out


def _customer_row(conn, customer_id: int, reseller_id: int | None = None):
    c = m.customers
    q = select(c).where(c.c.id == customer_id)
    if reseller_id is not None:
        q = q.where(c.c.reseller_id == reseller_id)
    return conn.execute(q).first()


def upsert_customer(conn, spec: dict, reseller_id: int | None = None) -> tuple[int, bool]:
    """The customer with this external_ref (for this reseller), updated, or
    a new one. Returns (id, created)."""
    data = clean_customer(spec)
    ref = (spec.get("external_ref") or "").strip()[:200] or None
    c = m.customers
    if ref is not None:
        q = select(c.c.id).where(c.c.external_ref == ref)
        q = q.where(c.c.reseller_id == reseller_id) if reseller_id is not None else q.where(c.c.reseller_id.is_(None))
        old = conn.execute(q).scalar()
        if old is not None:
            conn.execute(update(c).where(c.c.id == old).values(**data, updated_at=datetime.now(UTC)))
            return old, False
    new = conn.execute(insert(c).values(**data, external_ref=ref, reseller_id=reseller_id)
                       .returning(c.c.id)).scalar_one()
    return new, True


class DuplicateCustomer(BillingError):
    """A customer the console already has: its id, for a link to it."""

    def __init__(self, message: str, customer_id: int):
        super().__init__(message)
        self.customer_id = customer_id


TWINS = (("vat_number", N_("C'è già un cliente con questa partita IVA: {name}.")),
         ("tax_code", N_("C'è già un cliente con questo codice fiscale: {name}.")),
         ("email", N_("C'è già un cliente con questa email: {name}.")))


def twin_customer(conn, data: dict, but: int | None = None):
    """The customer with the same VAT number or tax code (or, for someone
    who has neither, the same email), and what they share: (row, message)
    or None. Two companies may share an accountant's email, not a VAT number."""
    c = m.customers
    for key, message in TWINS:
        if not data.get(key) or key == "email" and (data.get("vat_number") or data.get("tax_code")):
            continue
        q = select(c.c.id, c.c.name).where(c.c[key] == data[key])
        if but is not None:
            q = q.where(c.c.id != but)
        row = conn.execute(q.order_by(c.c.id).limit(1)).first()
        if row is not None:
            return row, message
    return None


def create_customer(admin, spec: dict, reseller_id: int | None = None) -> int:
    """A customer written by hand in the console, before or without a
    newsroom: checked like the site's, refused when its VAT number, tax
    code or email already belong to a customer (DuplicateCustomer)."""
    data = clean_customer(spec)
    with admin.engine.begin() as conn:
        if reseller_id is not None and conn.execute(select(m.resellers.c.id).where(
                m.resellers.c.id == reseller_id)).first() is None:
            raise BillingError(t("rivenditore sconosciuto"))
        twin = twin_customer(conn, data)
        if twin is not None:
            row, message = twin
            raise DuplicateCustomer(t(message, name=row.name), row.id)
        return conn.execute(insert(m.customers).values(**data, reseller_id=reseller_id)
                            .returning(m.customers.c.id)).scalar_one()


def update_customer(admin, customer_id: int, spec: dict, reseller_id: int | None = None) -> dict:
    data = clean_customer(spec, partial=True)
    with admin.engine.begin() as conn:
        if _customer_row(conn, customer_id, reseller_id) is None:
            raise LookupError(customer_id)
        if data:
            conn.execute(update(m.customers).where(m.customers.c.id == customer_id)
                         .values(**data, updated_at=datetime.now(UTC)))
    return customer(admin, customer_id)


def delete_customer(admin, customer_id: int) -> str:
    """Remove a customer that has no newsroom any more (its data are then
    no longer needed for invoices to come)."""
    with admin.engine.begin() as conn:
        row = _customer_row(conn, customer_id)
        if row is None:
            raise LookupError(customer_id)
        rooms = conn.execute(select(func.count()).select_from(m.tenants).where(
            m.tenants.c.customer_id == customer_id)).scalar()
        if rooms:
            raise BillingError(t("{name} ha ancora {n} redazioni: eliminale prima, o collegale a un altro cliente",
                                 name=row.name, n=rooms))
        conn.execute(delete(m.customers).where(m.customers.c.id == customer_id))
    return row.name


def customer(admin, customer_id: int, reseller_id: int | None = None) -> dict | None:
    with accounts._read(admin) as conn:
        row = _customer_row(conn, customer_id, reseller_id)
        if row is None:
            return None
        rooms = conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.customer_id == customer_id)
                             .order_by(m.tenants.c.tenant_id)).scalars().all()
    return {**dict(row._mapping), "tenants": list(rooms)}


def customers(admin, reseller_id: int | None = None, external_ref: str | None = None) -> list[dict]:
    """Every customer (of a reseller) with its newsrooms and subscriptions."""
    c, t, s = m.customers, m.tenants, m.subscriptions
    q = select(c).order_by(c.c.name)
    if reseller_id is not None:
        q = q.where(c.c.reseller_id == reseller_id)
    if external_ref is not None:
        q = q.where(c.c.external_ref == external_ref)
    with accounts._read(admin) as conn:
        rows = conn.execute(q).all()
        rooms = conn.execute(select(t.c.tenant_id, t.c.name, t.c.active, t.c.hold, t.c.customer_id, s.c.plan_code,
                                    s.c.status, s.c.trial_ends_at, s.c.current_period_end, s.c.provider,
                                    s.c.billing_interval)
                             .outerjoin(s, s.c.tenant_id == t.c.tenant_id)
                             .where(t.c.customer_id.is_not(None)).order_by(t.c.name)).all()
    by = {}
    for r in rooms:
        by.setdefault(r.customer_id, []).append(dict(r._mapping))
    return [{**dict(r._mapping), "newsrooms": by.get(r.id, [])} for r in rows]


def customer_of(admin, tenant_id: str) -> dict | None:
    with accounts._read(admin) as conn:
        cid = conn.execute(select(m.tenants.c.customer_id).where(m.tenants.c.tenant_id == tenant_id)).scalar()
    return customer(admin, cid) if cid is not None else None


def set_tenant_customer(admin, tenant_id: str, spec: dict) -> int:
    """Billing data of a newsroom, from its owner: the customer it has,
    updated, or a new one."""
    with admin.engine.begin() as conn:
        t = conn.execute(select(m.tenants.c.customer_id).where(m.tenants.c.tenant_id == tenant_id)).first()
        if t is None:
            raise LookupError(tenant_id)
        if t.customer_id is not None:
            data = clean_customer(spec)
            conn.execute(update(m.customers).where(m.customers.c.id == t.customer_id)
                         .values(**data, updated_at=datetime.now(UTC)))
            return t.customer_id
        cid, _ = upsert_customer(conn, spec)
        conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(customer_id=cid))
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id).values(customer_id=cid))
        return cid


def link_customer(admin, tenant_id: str, customer_id: int | None) -> None:
    """The console ties a newsroom to a customer it already has (one made
    before billing existed, or a second newsroom of the same customer), or
    unties it (None)."""
    with admin.engine.begin() as conn:
        if customer_id is not None and conn.execute(select(m.customers.c.id).where(
                m.customers.c.id == customer_id)).first() is None:
            raise BillingError(t("cliente sconosciuto"))
        if not conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id)
                            .values(customer_id=customer_id)).rowcount:
            raise LookupError(tenant_id)
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id)
                     .values(customer_id=customer_id))


def reseller_of_tenant(admin, tenant_id: str) -> int | None:
    with accounts._read(admin) as conn:
        return conn.execute(select(m.customers.c.reseller_id).join(m.tenants, m.tenants.c.customer_id == m.customers.c.id)
                            .where(m.tenants.c.tenant_id == tenant_id)).scalar()


def tenants_of_reseller(admin, reseller_id: int) -> set[str]:
    with accounts._read(admin) as conn:
        return set(conn.execute(select(m.tenants.c.tenant_id).join(m.customers, m.tenants.c.customer_id == m.customers.c.id)
                                .where(m.customers.c.reseller_id == reseller_id)).scalars())


# --- resellers -----------------------------------------------------------------------

def create_reseller(admin, name: str, email: str, discount_pct: float = 0, notes: str = "") -> int:
    name = (name or "").strip()[:100]
    if not name:
        raise BillingError(t("il rivenditore ha bisogno di un nome"))
    if not 0 <= discount_pct <= 100:
        raise BillingError(t("lo sconto è una percentuale da 0 a 100"))
    with admin.engine.begin() as conn:
        return conn.execute(insert(m.resellers).values(name=name, email=accounts.normalise_email(email),
                                                       discount_pct=discount_pct, notes=(notes or "")[:1000],
                                                       active=True).returning(m.resellers.c.id)).scalar_one()


def update_reseller(admin, reseller_id: int, fields: dict) -> None:
    values = {k: v for k, v in fields.items() if k in ("name", "email", "discount_pct", "notes", "active") and v is not None}
    if "email" in values:
        values["email"] = accounts.normalise_email(values["email"])
    if "discount_pct" in values and not 0 <= values["discount_pct"] <= 100:
        raise BillingError(t("lo sconto è una percentuale da 0 a 100"))
    with admin.engine.begin() as conn:
        if not conn.execute(update(m.resellers).where(m.resellers.c.id == reseller_id).values(**values)).rowcount:
            raise LookupError(reseller_id)
        if values.get("active") is False:  # its keys stop at once
            conn.execute(update(m.operator_tokens).where(m.operator_tokens.c.reseller_id == reseller_id,
                                                         m.operator_tokens.c.revoked_at.is_(None))
                         .values(revoked_at=datetime.now(UTC)))


def delete_reseller(admin, reseller_id: int) -> str:
    """Remove a reseller without customers, with its API keys."""
    with admin.engine.begin() as conn:
        r = conn.execute(select(m.resellers.c.name).where(m.resellers.c.id == reseller_id)).first()
        if r is None:
            raise LookupError(reseller_id)
        n = conn.execute(select(func.count()).select_from(m.customers).where(
            m.customers.c.reseller_id == reseller_id)).scalar()
        if n:
            raise BillingError(t("{name} ha {n} clienti: disattivalo invece di eliminarlo", name=r.name, n=n))
        conn.execute(delete(m.operator_tokens).where(m.operator_tokens.c.reseller_id == reseller_id))
        conn.execute(delete(m.resellers).where(m.resellers.c.id == reseller_id))
    return r.name


def resellers(admin) -> list[dict]:
    r, c = m.resellers, m.customers
    with accounts._read(admin) as conn:
        rows = conn.execute(select(r).order_by(r.c.name)).all()
        counts = dict(conn.execute(select(c.c.reseller_id, func.count()).where(c.c.reseller_id.is_not(None))
                                   .group_by(c.c.reseller_id)).all())
    return [{**dict(x._mapping), "customers": counts.get(x.id, 0)} for x in rows]


def reseller(admin, reseller_id: int):
    with accounts._read(admin) as conn:
        return conn.execute(select(m.resellers).where(m.resellers.c.id == reseller_id)).first()


# --- subscriptions -------------------------------------------------------------------

def subscription(admin, tenant_id: str) -> dict | None:
    s, p = m.subscriptions, m.plans
    with accounts._read(admin) as conn:
        row = conn.execute(select(s, p.c.name.label("plan_name"), p.c.price_month_cents, p.c.price_year_cents,
                                  p.c.currency).join(p, p.c.code == s.c.plan_code)
                           .where(s.c.tenant_id == tenant_id)).first()
    return describe(row) if row is not None else None


def subscriptions(admin) -> dict[str, dict]:
    """Every newsroom's subscription, by tenant id (the console)."""
    s, p = m.subscriptions, m.plans
    with accounts._read(admin) as conn:
        rows = conn.execute(select(s, p.c.name.label("plan_name"), p.c.price_month_cents, p.c.price_year_cents,
                                   p.c.currency).join(p, p.c.code == s.c.plan_code)).all()
    return {r.tenant_id: describe(r) for r in rows}


def describe(row, now: datetime | None = None) -> dict:
    """A subscription with what a page says about it."""
    out = dict(row._mapping)
    now = _now(now)
    out["label"] = t(STATUS_LABELS[out["status"]]) if out["status"] in STATUS_LABELS else out["status"]
    if out["status"] == "expired" and out.get("desired_plan") and out["desired_plan"] == out["plan_code"]:
        out["label"] = t("Da pagare")  # bought on the site, waiting for its payment
    # What the newsroom brings in a month, at list price: nothing on trial or unpaid.
    month, year = out.get("price_month_cents"), out.get("price_year_cents")
    out["monthly_cents"] = None if out["status"] != "active" else (
        round(year / 12) if out.get("billing_interval") == "year" and year is not None else month)
    out["days_left"] = None
    end = out["trial_ends_at"] if out["status"] == "trialing" else out["current_period_end"]
    if end is not None:
        out["days_left"] = max(0, (end - now).days + (1 if (end - now).seconds else 0))
    return out


def bought_unpaid(admin, user_id: int) -> str | None:
    """A newsroom this person owns, bought on the site ("Acquista") and not
    paid yet: on trial (or with the trial refused) and the plan they picked
    waiting. Signing in goes straight to its payment until it is paid."""
    for x in accounts.memberships(admin, user_id):
        if x.role != "owner":
            continue
        sub = subscription(admin, x.tenant_id)
        if sub and sub["status"] in ("trialing", "expired") and sub.get("desired_plan"):
            return x.tenant_id
    return None


def on_hold(admin, tenant_id: str) -> bool:
    with accounts._read(admin) as conn:
        return conn.execute(select(m.tenants.c.hold).where(m.tenants.c.tenant_id == tenant_id)).scalar() == "billing"


def _hold(conn, tenant_id: str) -> None:
    conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(active=False, hold="billing"))


def _release(conn, tenant_id: str) -> None:
    conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id, m.tenants.c.hold == "billing")
                 .values(active=True, hold=None))
    conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(proposals_only=None))


def _proposals_only(conn, tenant_id: str, now: datetime) -> None:
    conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(proposals_only=now))


def _leave_the_gateway(admin, tenant_id: str, sub: dict, values: dict, gateway) -> None:
    """The subscription stops being paid at the gateway: its automatic
    renewals are cancelled and its id forgotten. Without this the gateway
    would charge again at the next renewal and the notice would extend the
    period a second time: the customer would pay twice for one period."""
    if sub.get("provider") != "mollie" or not sub.get("provider_subscription_id"):
        return
    if sub.get("status") == "canceled":  # renewals already stopped at Mollie (a refund, a cancel)
        values["provider_subscription_id"] = None
        return
    if gateway is None:
        raise BillingError(t("il pagamento online non è configurato: i rinnovi su Mollie restano attivi"))
    customer = customer_of(admin, tenant_id)
    if customer is not None:
        try:
            gateway.cancel_subscription(customer["provider_customer_id"], sub["provider_subscription_id"])
        except Exception as e:
            log.exception("the automatic renewals of %s were not cancelled", tenant_id)
            raise BillingError(t("i rinnovi automatici su Mollie non sono stati annullati, riprova")) from e
    values["provider_subscription_id"] = None


def start(admin, tenant_id: str, plan_code: str, customer_id: int | None = None, provider: str = "manual",
          interval: str = "month", paid_until: datetime | None = None, core=None, now: datetime | None = None,
          gateway=None) -> dict:
    """The newsroom's subscription: a plan with a trial starts its trial,
    any other plan is active (until `paid_until`, or open-ended when not
    given: a reseller or the operator decides). Replaces an existing one."""
    now = _now(now)
    if interval not in INTERVALS:
        raise BillingError(t("l'intervallo è month o year"))
    if provider not in PROVIDERS:
        raise BillingError(t("provider: uno di {names}", names=", ".join(PROVIDERS)))
    old = subscription(admin, tenant_id)
    with admin.engine.begin() as conn:
        plan = get_plan(conn, plan_code)
        if conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.tenant_id == tenant_id)).first() is None:
            raise LookupError(tenant_id)
        trial = plan.trial_days > 0
        values = {"plan_code": plan.code, "billing_interval": interval, "provider": provider,
                  "status": "trialing" if trial else "active",
                  "trial_ends_at": now + timedelta(days=plan.trial_days) if trial else None,
                  # Without a date, a period already paid and not over yet is kept.
                  "current_period_end": None if trial else (paid_until or (
                      end if (end := (old or {}).get("current_period_end")) and end > now else None)),
                  "reminded_at": None, "updated_at": now}
        if not trial:  # the plan wished for during the trial is now the plan
            values |= {"desired_plan": None, "desired_interval": None}
        if customer_id is not None:
            values["customer_id"] = customer_id
        s = m.subscriptions
        if conn.execute(select(s.c.id).where(s.c.tenant_id == tenant_id)).first() is None:
            conn.execute(insert(s).values(tenant_id=tenant_id, created_at=now, **values))
        else:
            if old is not None and old.get("provider") != provider:
                _leave_the_gateway(admin, tenant_id, old, values, gateway)
            conn.execute(update(s).where(s.c.tenant_id == tenant_id).values(**values))
        if customer_id is not None:
            conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(customer_id=customer_id))
        if paid_until is None or paid_until > now or trial:
            _release(conn, tenant_id)
    apply_plan(admin, tenant_id, plan, core)
    return subscription(admin, tenant_id)


def record_payment(admin, tenant_id: str, amount_cents: int, until: datetime | None, by: str, note: str = "",
                   plan_code: str | None = None, interval: str | None = None, core=None,
                   now: datetime | None = None, gateway=None) -> dict:
    """A payment the operator received (a bank transfer): the plan becomes
    active until `until` (by default one interval after the current end)."""
    now = _now(now)
    sub = subscription(admin, tenant_id)
    if sub is None:
        raise LookupError(tenant_id)
    interval = interval or sub["billing_interval"]
    values = {"plan_code": sub["plan_code"], "billing_interval": interval, "status": "active",
              "current_period_end": None, "trial_ends_at": None, "updated_at": now,
              "desired_plan": None, "desired_interval": None}
    with admin.engine.begin() as conn:
        plan = get_plan(conn, plan_code or (sub["plan_code"] if sub["plan_code"] != TRIAL_PLAN else "starter"))
        base = max(sub["current_period_end"] or now, now)
        until = until or add_interval(base, interval)
        values["plan_code"] = plan.code
        values["current_period_end"] = until
        if sub["provider"] == "mollie":
            values["provider"] = "manual"
            _leave_the_gateway(admin, tenant_id, sub, values, gateway)
        conn.execute(insert(m.payments).values(tenant_id=tenant_id, subscription_id=sub["id"], provider="manual",
                                               kind="manual", amount_cents=amount_cents, status="paid",
                                               note=f"{by}: {note}"[:500], paid_at=now))
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id).values(**values))
        _release(conn, tenant_id)
    apply_plan(admin, tenant_id, plan, core)
    return subscription(admin, tenant_id)


def cancel(admin, tenant_id: str, gateway=None, now: datetime | None = None) -> dict:
    """No more renewals: the newsroom keeps working until the end of the
    period already paid, then goes on hold."""
    now = _now(now)
    sub = subscription(admin, tenant_id)
    if sub is None:
        raise LookupError(tenant_id)
    if sub["provider"] == "mollie" and sub["provider_subscription_id"] and gateway is not None:
        c = customer_of(admin, tenant_id)
        gateway.cancel_subscription(c["provider_customer_id"], sub["provider_subscription_id"])
    with admin.engine.begin() as conn:
        values = {"status": "canceled", "updated_at": now}
        if sub["status"] == "trialing":  # nothing paid: it ends with the trial
            values["current_period_end"] = sub["trial_ends_at"]
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id).values(**values))
    return subscription(admin, tenant_id)


def owners(admin, tenant_id: str) -> list[str]:
    return [p.email for p in accounts.members(admin, tenant_id) if p.role == "owner"]


def lifecycle(admin, now: datetime | None = None, mail=None, public_url: str = "") -> dict:
    """Run every hour by the worker: reminders before a trial ends,
    proposals only when a trial or a paid period ends, the hold
    PROPOSALS_ONLY_DAYS later. Returns what it did."""
    now = _now(now)
    s, tn = m.subscriptions, m.tenants
    report = {"reminded": [], "proposals_only": [], "held": []}
    with accounts._read(admin) as conn:
        rows = conn.execute(select(s.c.tenant_id, s.c.status, s.c.trial_ends_at, s.c.current_period_end,
                                   s.c.reminded_at, tn.c.name, tn.c.hold, tn.c.language)
                            .join(tn, tn.c.tenant_id == s.c.tenant_id)
                            .where(s.c.status != "expired")).all()
    link = f"{public_url.rstrip('/')}/account" if public_url else "/account"
    for r in rows:
        ends = r.trial_ends_at if r.status == "trialing" else r.current_period_end
        if ends is None:
            continue
        if r.status == "trialing" and ends - timedelta(days=REMIND_DAYS) <= now < ends and r.reminded_at is None:
            if mail is not None:
                # No request here: the newsroom's own language.
                lang = r.language if r.language in i18n.LANGS else i18n.DEFAULT
                for to in owners(admin, r.tenant_id):
                    try:
                        mail(to, t("La prova di {name} finisce il {date}", lang, name=r.name, date=f"{ends:%d/%m}"),
                             t("La prova gratuita della redazione {name} finisce il {date}.", lang, name=r.name,
                               date=f"{ends:%d/%m/%Y}") + "\n\n"
                             + t("Per continuare senza interruzioni scegli un piano:", lang) + f"\n\n{link}\n\n"
                             + t("Se non scegli un piano la redazione si ferma, ma i tuoi dati restano e "
                                 "puoi riattivarla quando vuoi.", lang) + "\n")
                    except Exception:
                        log.exception("trial reminder to %s failed", to)
            with admin.engine.begin() as conn:
                conn.execute(update(s).where(s.c.tenant_id == r.tenant_id).values(reminded_at=now))
            report["reminded"].append(r.tenant_id)
        grace = timedelta(0) if r.status == "trialing" else timedelta(days=GRACE_DAYS)
        if ends + grace <= now:
            with admin.engine.begin() as conn:
                conn.execute(update(s).where(s.c.tenant_id == r.tenant_id).values(status="expired", updated_at=now))
                _proposals_only(conn, r.tenant_id, now)
            report["proposals_only"].append(r.tenant_id)
            log.info("newsroom %s on proposals only: %s ended", r.tenant_id,
                     "trial" if r.status == "trialing" else "paid period")
            if mail is not None:
                lang = r.language if r.language in i18n.LANGS else i18n.DEFAULT
                stop = now + timedelta(days=PROPOSALS_ONLY_DAYS)
                for to in owners(admin, r.tenant_id):
                    try:
                        mail(to, t("La prova di {name} è finita", lang, name=r.name)
                             if r.status == "trialing" else t("Il periodo pagato di {name} è finito", lang, name=r.name),
                             t("Da oggi la redazione {name} ti manda solo le proposte di articolo, due al giorno "
                               "per sito: niente bozze e niente pubblicazioni.", lang, name=r.name) + "\n\n"
                             + t("Scegli un piano e riparte tutto, con i tuoi siti, profili e fonti:", lang)
                             + f"\n\n{link}\n\n"
                             + t("Senza un piano la redazione si ferma il {date}; i tuoi dati restano.", lang,
                                 date=f"{stop:%d/%m/%Y}") + "\n")
                    except Exception:
                        log.exception("end of trial email to %s failed", to)
    # Proposals only for long enough without a plan: the newsroom stops.
    with accounts._read(admin) as conn:
        stale = conn.execute(select(tn.c.tenant_id).where(
            tn.c.proposals_only.is_not(None),
            tn.c.proposals_only <= now - timedelta(days=PROPOSALS_ONLY_DAYS))).scalars().all()
    for tenant_id in stale:
        with admin.engine.begin() as conn:
            conn.execute(update(tn).where(tn.c.tenant_id == tenant_id).values(proposals_only=None))
            _hold(conn, tenant_id)
        report["held"].append(tenant_id)
        log.info("newsroom %s on hold: %d days of proposals only", tenant_id, PROPOSALS_ONLY_DAYS)
    return report


def _trial_once(admin, tenant_id: str, customer_id: int, customer: dict, owner_email: str,
                now: datetime | None) -> list[dict]:
    """The trial is given once: with the data of an earlier signup the
    newsroom waits for a plan in /account at once. Returns what matched."""
    from . import trials

    now = _now(now)
    pairs = trials.keys(customer, owner_email)
    with admin.engine.begin() as conn:
        matches = trials.earlier(conn, pairs, tenant_id, customer_id)
        trials.mark(conn, pairs, tenant_id, customer_id)
        if matches:
            conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id)
                         .values(status="expired", trial_ends_at=now, updated_at=now))
            _hold(conn, tenant_id)
            trials.refused(conn, tenant_id, customer_id, matches)
            log.info("newsroom %s without trial: %s used before", tenant_id, ", ".join(x["kind"] for x in matches))
    return [{"kind": x["kind"]} for x in matches]


def signup(admin, spec: dict, by: str, reseller_id: int | None = None, core=None, now: datetime | None = None) -> dict:
    """A new customer from the public site (or a reseller): billing data,
    a newsroom on `plan` (the trial by default) and its owner invited.
    spec: customer{...}, newsroom (name), owner_email (default: the
    customer's email), owner_name, plan, interval, external_ref.
    With an external_ref already known nothing new is made."""
    cust = dict(spec.get("customer") or {})
    ref = (spec.get("external_ref") or cust.get("external_ref") or "").strip()[:200] or None
    if ref:
        cust["external_ref"] = ref
    plan_code = spec.get("plan") or TRIAL_PLAN
    interval = spec.get("interval") or "month"
    # From the public site a paid plan starts only with its payment: the
    # newsroom is made on that plan, stopped, with no trial, and waits for
    # the payment in /account. A reseller bills its customers itself, so its
    # plan starts at once.
    desired = None
    if reseller_id is None and plan_code != TRIAL_PLAN:
        desired, plan_code = plan_code, TRIAL_PLAN
    name = (spec.get("newsroom") or cust.get("name") or "").strip()
    with admin.engine.begin() as conn:
        get_plan(conn, plan_code)
        if desired:
            get_plan(conn, desired)
        cid, created = upsert_customer(conn, cust, reseller_id)
    room_ref = f"{'r' + str(reseller_id) if reseller_id is not None else 'site'}:{ref}" if ref else None
    lang = spec.get("language") or i18n.current()
    res = newsrooms.create(admin, {"name": name, "owner_email": spec.get("owner_email") or cust["email"],
                                   "owner_name": spec.get("owner_name"), "external_ref": room_ref,
                                   "language": lang}, by, core)
    tid = res["tenant_id"]
    trial_used: list[dict] = []
    if res["created"] or subscription(admin, tid) is None:
        provider = "reseller" if reseller_id is not None else "manual"
        sub = start(admin, tid, plan_code, cid, provider, interval, core=core, now=now)
        if res["created"] and sub["status"] == "trialing" and not desired:
            trial_used = _trial_once(admin, tid, cid, clean_customer(cust), res["owner_email"], now)
    if desired:
        with admin.engine.begin() as conn:
            s = m.subscriptions
            if conn.execute(update(s).where(s.c.tenant_id == tid, s.c.status.in_(("trialing", "expired"))).values(
                    plan_code=desired, billing_interval=interval, status="expired", trial_ends_at=_now(now),
                    desired_plan=desired, desired_interval=interval, updated_at=_now(now))).rowcount:
                _hold(conn, tid)
    sub = subscription(admin, tid)
    return {"customer_id": cid, "customer_created": created, "tenant_id": tid, "created": res["created"],
            "owner_email": res["owner_email"], "invite": res["invite"], "warning": res["warning"],
            "existing_user": res.get("existing_user", False), "trial_used": trial_used,
            "plan": sub["plan_code"] if sub else None, "status": sub["status"] if sub else None,
            "desired_plan": sub.get("desired_plan") if sub else None}


# --- payments through a gateway ------------------------------------------------------

def checkout(admin, gateway, tenant_id: str, plan_code: str, interval: str, redirect_url: str, webhook_url: str,
             now: datetime | None = None) -> str:
    """Start paying a plan: the address of the gateway's payment page. The
    first payment also authorises the renewals (a mandate)."""
    if gateway is None:
        raise BillingError(t("pagamenti online non configurati: contatta chi ti ha venduto il servizio"))
    if interval not in INTERVALS:
        raise BillingError(t("l'intervallo è month o year"))
    sub = subscription(admin, tenant_id)
    if sub is not None and sub["provider"] == "reseller":
        raise BillingError(t("questa redazione è fatturata dal tuo rivenditore"))
    c = customer_of(admin, tenant_id)
    if c is None:
        raise BillingError(t("prima servono i dati di fatturazione"))
    with accounts._read(admin) as conn:
        plan = get_plan(conn, plan_code)
    amount = price(plan, interval)
    if not amount:
        raise BillingError(t("questo piano non si paga online"))
    amount += addons_amount(admin, tenant_id, interval)  # the extra languages already bought go on with the plan
    cust_id = c["provider_customer_id"]
    if not cust_id:
        cust_id = gateway.create_customer(c["name"], c["email"], {"customer_id": c["id"]})
        with admin.engine.begin() as conn:
            conn.execute(update(m.customers).where(m.customers.c.id == c["id"]).values(provider_customer_id=cust_id))
    meta = {"tenant_id": tenant_id, "plan": plan.code, "interval": interval}
    period = t("anno") if interval == "year" else t("mese")
    amount, vat = with_vat(amount, c)
    pay_id, url = gateway.first_payment(cust_id, amount, plan.currency, f"Redazia {plan.name} ({period})",
                                        redirect_url, webhook_url, meta)
    with admin.engine.begin() as conn:
        conn.execute(insert(m.payments).values(tenant_id=tenant_id, subscription_id=sub["id"] if sub else None,
                                               provider=gateway.name, provider_payment_id=pay_id, kind="first",
                                               amount_cents=amount, vat_cents=vat, currency=plan.currency,
                                               status="open", note=f"{plan.code}/{interval}", created_at=_now(now)))
    return url


def buy_credits(admin, gateway, tenant_id: str, pack_code: str, redirect_url: str, webhook_url: str,
                now: datetime | None = None) -> str:
    """Start paying a pack of credits: the address of the gateway's page.
    The credits are added when the gateway says the payment is paid."""
    if gateway is None:
        raise BillingError(t("pagamenti online non configurati: contatta chi ti ha venduto il servizio"))
    c = customer_of(admin, tenant_id)
    if c is None:
        raise BillingError(t("prima servono i dati di fatturazione"))
    with accounts._read(admin) as conn:
        pack = credits.get_pack(conn, pack_code)
    if not pack.active or not pack.price_cents:
        raise BillingError(t("questo pacchetto non è in vendita"))
    cust_id = c["provider_customer_id"]
    if not cust_id:
        cust_id = gateway.create_customer(c["name"], c["email"], {"customer_id": c["id"]})
        with admin.engine.begin() as conn:
            conn.execute(update(m.customers).where(m.customers.c.id == c["id"]).values(provider_customer_id=cust_id))
    sub = subscription(admin, tenant_id)
    gross, vat = with_vat(pack.price_cents, c)
    pay_id, url = gateway.one_off_payment(cust_id, gross, pack.currency, f"Redazia {pack.name}",
                                          redirect_url, webhook_url,
                                          {"tenant_id": tenant_id, "pack": pack.code, "credits": pack.credits})
    with admin.engine.begin() as conn:
        conn.execute(insert(m.payments).values(tenant_id=tenant_id, subscription_id=sub["id"] if sub else None,
                                               provider=gateway.name, provider_payment_id=pay_id, kind="credits",
                                               amount_cents=gross, vat_cents=vat, currency=pack.currency, status="open",
                                               note=f"{pack.code}:{pack.credits}", created_at=_now(now)))
    return url


def record_credits(admin, tenant_id: str, credits_n: int, amount_cents: int, by: str, note: str = "",
                   now: datetime | None = None) -> int:
    """Credits paid outside the gateway (a bank transfer) or given: the
    payment row (when there is an amount) and the credits."""
    if amount_cents < 0:
        raise BillingError(t("l'importo è un numero di zero o più"))
    ref = None
    if amount_cents:
        with admin.engine.begin() as conn:
            pid = conn.execute(insert(m.payments).values(
                tenant_id=tenant_id, provider="manual", kind="credits", amount_cents=amount_cents, currency="EUR",
                status="paid", note=(note or f"{credits_n} crediti")[:200], created_at=_now(now),
                paid_at=_now(now)).returning(m.payments.c.id)).scalar_one()
        ref = f"payment:{pid}"
    return credits.grant(admin, tenant_id, credits_n, "pack" if amount_cents else "grant", note, by, ref)


def gateway_event(admin, gateway, payment_id: str, webhook_url: str, core=None, now: datetime | None = None) -> str:
    """The gateway says a payment changed: read it from the gateway (the
    notice itself carries only its id) and act. Returns what was done."""
    now = _now(now)
    pay = gateway.payment(payment_id)
    if pay is None:
        return "unknown"
    p, s = m.payments, m.subscriptions
    with accounts._read(admin) as conn:
        ours = conn.execute(select(p).where(p.c.provider_payment_id == payment_id)).first()
        sub_row = None
        if ours is None and pay.get("subscription_id"):
            sub_row = conn.execute(select(s).where(s.c.provider_subscription_id == pay["subscription_id"])).first()
    if ours is None and sub_row is None:
        return "unknown"
    if ours is None:  # a renewal Mollie made on its own
        with admin.engine.begin() as conn:
            conn.execute(insert(p).values(tenant_id=sub_row.tenant_id, subscription_id=sub_row.id, provider=gateway.name,
                                          provider_payment_id=payment_id, kind="recurring",
                                          amount_cents=pay["amount_cents"], currency=pay["currency"], status="open",
                                          vat_cents=vat_in(pay["amount_cents"], customer_of(admin, sub_row.tenant_id)),
                                          created_at=now))
            ours = conn.execute(select(p).where(p.c.provider_payment_id == payment_id)).first()
    status = pay["status"]
    if ours.status == status or ours.status == "paid":
        return "unchanged"
    with admin.engine.begin() as conn:
        conn.execute(update(p).where(p.c.id == ours.id).values(status=status, paid_at=now if status == "paid" else None))
    tenant_id = ours.tenant_id
    if ours.kind == "addon":
        if status != "paid" or tenant_id is None:
            return status
        site_id = int((pay.get("metadata") or {}).get("site_id") or (ours.note or "0").rpartition(":")[2] or 0)
        _add_language(admin, gateway, tenant_id, site_id, +1)
        return "addon"
    if ours.kind == "credits":
        if status != "paid" or tenant_id is None:
            return status
        n = int((pay.get("metadata") or {}).get("credits") or (ours.note or "0").rpartition(":")[2] or 0)
        credits.grant(admin, tenant_id, n, "pack", ours.note or "", "mollie", f"payment:{ours.id}")
        return "credits"
    sub = subscription(admin, tenant_id)
    if status == "paid" and ours.kind == "first":
        meta = pay.get("metadata") or {}
        plan_code, interval = meta.get("plan") or "starter", meta.get("interval") or "month"
        until = add_interval(now, interval)
        c = customer_of(admin, tenant_id)
        if sub and sub["provider"] == "mollie" and sub["provider_subscription_id"]:  # a change of plan
            try:
                gateway.cancel_subscription(c["provider_customer_id"], sub["provider_subscription_id"])
            except Exception:
                log.exception("old subscription of %s not cancelled", tenant_id)
        with accounts._read(admin) as conn:
            plan = get_plan(conn, plan_code)
        amount, _ = with_vat(price(plan, interval) + addons_amount(admin, tenant_id, interval), c)
        sub_id = gateway.create_subscription(c["provider_customer_id"], amount, plan.currency, interval,
                                             until.date(), f"Redazia {plan.name}", webhook_url,
                                             pay.get("mandate_id"), {"tenant_id": tenant_id})
        with admin.engine.begin() as conn:
            conn.execute(update(s).where(s.c.tenant_id == tenant_id).values(
                plan_code=plan.code, billing_interval=interval, status="active", provider="mollie",
                provider_subscription_id=sub_id, renewal_cents=amount, trial_ends_at=None, current_period_end=until, updated_at=now))
            _release(conn, tenant_id)
        apply_plan(admin, tenant_id, plan, core)
        return "activated"
    if status == "paid":
        with admin.engine.begin() as conn:
            base = max(sub["current_period_end"] or now, now)
            conn.execute(update(s).where(s.c.tenant_id == tenant_id).values(
                status="active", current_period_end=add_interval(base, sub["billing_interval"]), updated_at=now))
            _release(conn, tenant_id)
        return "renewed"
    if status in ("failed", "expired", "canceled") and ours.kind == "recurring":
        with admin.engine.begin() as conn:
            conn.execute(update(s).where(s.c.tenant_id == tenant_id, s.c.status == "active")
                         .values(status="past_due", updated_at=now))
        return "past_due"
    return status


# --- add-ons: a language more for a site ------------------------------------------

LANGUAGE_ADDON = "language"


def addon(conn, code: str = LANGUAGE_ADDON):
    row = conn.execute(select(m.addons).where(m.addons.c.code == code)).first()
    if row is None:
        raise BillingError(t("componente aggiuntivo sconosciuto: {code}", code=code))
    return row


def addons(admin) -> list[dict]:
    with accounts._read(admin) as conn:
        return [dict(r._mapping) for r in conn.execute(select(m.addons).order_by(m.addons.c.code)).all()]


def save_addon(admin, code: str, price_month_cents: int | None, price_year_cents: int | None) -> None:
    for v in (price_month_cents, price_year_cents):
        if v is not None and v < 0:
            raise BillingError(t("i prezzi sono numeri di zero o più"))
    with admin.engine.begin() as conn:
        if not conn.execute(update(m.addons).where(m.addons.c.code == code).values(
                price_month_cents=price_month_cents, price_year_cents=price_year_cents)).rowcount:
            raise BillingError(t("componente aggiuntivo sconosciuto: {code}", code=code))


def _extras(conn, tenant_id: str) -> dict[int, int]:
    raw = conn.execute(select(m.tenants.c.extra_languages)
                       .where(m.tenants.c.tenant_id == tenant_id)).scalar() or {}
    return {int(k): int(v) for k, v in raw.items() if int(v or 0) > 0}


def extra_languages(admin, tenant_id: str) -> dict[int, int]:
    """The languages a newsroom bought beyond its plan, by site."""
    with accounts._read(admin) as conn:
        return _extras(conn, tenant_id)


def addons_amount(admin, tenant_id: str, interval: str) -> int:
    """What the extra languages add to each renewal of the plan."""
    with accounts._read(admin) as conn:
        n = sum(_extras(conn, tenant_id).values())
        if not n:
            return 0
        a = addon(conn)
    each = a.price_year_cents if interval == "year" else a.price_month_cents
    return n * (each or 0)


def prorated(full_cents: int, period_end: datetime, interval: str, now: datetime) -> int:
    """The part of a period's price still to come: a language bought in the
    middle of a paid period pays only what is left of it, then renews with
    the plan."""
    months = 12 if interval == "year" else 1
    y, mo = divmod(period_end.month - 1 - months, 12)
    year, month = period_end.year + y, mo + 1
    start = period_end.replace(year=year, month=month,
                               day=min(period_end.day, calendar.monthrange(year, month)[1]))
    whole = (period_end - start).total_seconds()
    left = max(0.0, (period_end - now).total_seconds())
    return max(1, round(full_cents * min(1.0, left / whole))) if whole > 0 else full_cents


def language_offer(admin, tenant_id: str, now: datetime | None = None) -> dict:
    """Whether the newsroom can buy a language online now, and at what price:
    {"ok": bool, "why": str, "price": cents per period, "interval",
    "now": cents to pay today, "currency"}."""
    now = _now(now)
    sub = subscription(admin, tenant_id)
    with accounts._read(admin) as conn:
        a = addon(conn)
    interval = (sub or {}).get("billing_interval") or "month"
    each = a.price_year_cents if interval == "year" else a.price_month_cents
    out = {"ok": False, "why": "", "price": each, "interval": interval, "now": None, "currency": a.currency,
           "month": a.price_month_cents, "year": a.price_year_cents}
    if not each:
        out["why"] = t("le lingue extra non sono in vendita")
    elif sub is None or sub["status"] in ("trialing", "expired") or sub["plan_code"] == TRIAL_PLAN:
        out["why"] = t("scegli prima un piano in Il mio piano")
    elif sub["provider"] == "reseller":
        out["why"] = t("questa redazione è fatturata dal tuo rivenditore")
    elif sub["provider"] != "mollie" or not sub.get("provider_subscription_id") or sub["status"] != "active":
        out["why"] = t("il piano non si rinnova con la carta: chiedi la lingua a chi ti fattura")
    elif not sub.get("current_period_end") or sub["current_period_end"] <= now:
        out["why"] = t("il periodo pagato è finito: rinnova prima il piano")
    else:
        out["ok"] = True
        out["now"] = prorated(each, sub["current_period_end"], interval, now)
    return out


def buy_language(admin, gateway, tenant_id: str, site_id: int, site_name: str, redirect_url: str, webhook_url: str,
                 now: datetime | None = None) -> str:
    """Start paying a language more for a site: the gateway's page. The
    first payment is what is left of the current period; from the next
    renewal the language is in the plan's amount."""
    if gateway is None:
        raise BillingError(t("pagamenti online non configurati: contatta chi ti ha venduto il servizio"))
    offer = language_offer(admin, tenant_id, now)
    if not offer["ok"]:
        raise BillingError(offer["why"])
    c = customer_of(admin, tenant_id)
    if c is None or not c["provider_customer_id"]:
        raise BillingError(t("prima servono i dati di fatturazione"))
    sub = subscription(admin, tenant_id)
    gross, vat = with_vat(offer["now"], c)
    pay_id, url = gateway.one_off_payment(
        c["provider_customer_id"], gross, offer["currency"], f"Redazia {t('lingua extra')}: {site_name}"[:255],
        redirect_url, webhook_url, {"tenant_id": tenant_id, "addon": LANGUAGE_ADDON, "site_id": str(site_id)})
    with admin.engine.begin() as conn:
        conn.execute(insert(m.payments).values(tenant_id=tenant_id, subscription_id=sub["id"], provider=gateway.name,
                                               provider_payment_id=pay_id, kind="addon", amount_cents=gross,
                                               vat_cents=vat, currency=offer["currency"], status="open",
                                               note=f"{LANGUAGE_ADDON}:{site_id}", created_at=_now(now)))
    return url


def _add_language(admin, gateway, tenant_id: str, site_id: int, delta: int) -> int:
    """One language more (or less) for the site; the plan's renewals at
    the gateway follow. Returns the site's extra languages now."""
    with admin.engine.begin() as conn:
        raw = conn.execute(select(m.tenants.c.extra_languages).where(m.tenants.c.tenant_id == tenant_id)).scalar() or {}
        raw = {str(k): int(v) for k, v in raw.items()}
        n = max(0, raw.get(str(site_id), 0) + delta)
        if n:
            raw[str(site_id)] = n
        else:
            raw.pop(str(site_id), None)
        conn.execute(update(m.tenants).where(m.tenants.c.tenant_id == tenant_id).values(extra_languages=raw or None))
    _renewal_follows(admin, gateway, tenant_id)
    return n


def renewal_amount(admin, tenant_id: str) -> tuple[int, str] | None:
    """What each automatic renewal should charge now: the plan and its extra
    languages, VAT included (None without a renewal by card)."""
    sub = subscription(admin, tenant_id)
    if not sub or sub["provider"] != "mollie" or not sub.get("provider_subscription_id"):
        return None
    with accounts._read(admin) as conn:
        plan = get_plan(conn, sub["plan_code"])
    net = (price(plan, sub["billing_interval"]) or 0) + addons_amount(admin, tenant_id, sub["billing_interval"])
    return with_vat(net, customer_of(admin, tenant_id))[0], plan.currency


def _renewal_follows(admin, gateway, tenant_id: str, force: bool = True) -> bool:
    """Bring the amount of the plan's automatic renewals at the gateway to
    what they should charge (a language added or removed, the VAT, billing
    data that change it). Returns whether the gateway was changed."""
    if gateway is None:
        return False
    want = renewal_amount(admin, tenant_id)
    if want is None:
        return False
    amount, currency = want
    sub = subscription(admin, tenant_id)
    if not force and sub.get("renewal_cents") == amount:
        return False
    c = customer_of(admin, tenant_id)
    try:
        gateway.update_subscription(c["provider_customer_id"], sub["provider_subscription_id"], amount, currency)
    except Exception:
        # What is paid stays: the next hourly round tries again, and the
        # log says which renewals still charge the old amount.
        log.exception("the renewals of %s at the gateway were not changed to %s", tenant_id, amount)
        return False
    with admin.engine.begin() as conn:
        conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == tenant_id)
                     .values(renewal_cents=amount))
    return True


def sync_renewals(admin, gateway) -> list[str]:
    """Every hour: the renewals by card whose amount at the gateway is not
    the one they should charge (subscriptions made before the VAT, billing
    data changed) are brought to it. Returns the newsrooms changed."""
    if gateway is None:
        return []
    s = m.subscriptions
    with accounts._read(admin) as conn:
        rooms = conn.execute(select(s.c.tenant_id).where(
            s.c.provider == "mollie", s.c.provider_subscription_id.is_not(None),
            s.c.status == "active")).scalars().all()
    return [tid for tid in rooms if _renewal_follows(admin, gateway, tid, force=False)]


def remove_language(admin, gateway, tenant_id: str, site_id: int) -> int:
    """One extra language less for the site, from the next renewal (what is
    paid is not refunded). Returns the site's extra languages now."""
    if extra_languages(admin, tenant_id).get(site_id, 0) <= 0:
        raise BillingError(t("questo sito non ha lingue extra"))
    return _add_language(admin, gateway, tenant_id, site_id, -1)


def set_extra_languages(admin, tenant_id: str, site_id: int, n: int, gateway=None) -> int:
    """The console sets a site's extra languages by hand (a bank transfer,
    a gift); the renewals at the gateway follow."""
    if n < 0:
        raise BillingError(t("un numero di zero o più"))
    have = extra_languages(admin, tenant_id).get(site_id, 0)
    return _add_language(admin, gateway, tenant_id, site_id, n - have)


REFUND_THEN = ("none", "renewals", "now")


def payments_by_tenant(admin, per_tenant: int = 12) -> dict[str, list[dict]]:
    """The latest payments and refunds of each newsroom (the console), with
    how much of each paid online payment is still refundable."""
    p = m.payments
    with accounts._read(admin) as conn:
        rows = conn.execute(select(p).where(p.c.tenant_id.is_not(None))
                            .order_by(p.c.created_at.desc(), p.c.id.desc())).all()
    refunded: dict[str, int] = {}
    for r in rows:
        if r.kind == "refund" and r.status not in ("failed", "canceled"):
            of = (r.note or "").rpartition("tr_")[2].split()[0] if "tr_" in (r.note or "") else ""
            if of:
                refunded["tr_" + of] = refunded.get("tr_" + of, 0) + r.amount_cents
    out: dict[str, list[dict]] = {}
    for r in rows:
        items = out.setdefault(r.tenant_id, [])
        if len(items) >= per_tenant:
            continue
        d = dict(r._mapping)
        d["refundable_cents"] = 0
        if r.provider == "mollie" and r.status == "paid" and r.kind in ("first", "recurring") and r.provider_payment_id:
            d["refundable_cents"] = max(0, r.amount_cents - refunded.get(r.provider_payment_id, 0))
        items.append(d)
    return out


def all_payments(admin, limit: int = 2000) -> list[dict]:
    """Every payment and refund, newest first, with its newsroom and the
    newsroom's customer (the console's Payments page)."""
    p, tn, c = m.payments, m.tenants, m.customers
    with accounts._read(admin) as conn:
        rows = conn.execute(
            select(p, tn.c.name.label("newsroom"), c.c.id.label("customer_id"), c.c.name.label("customer_name"),
                   c.c.vat_number.label("customer_vat"), c.c.tax_code.label("customer_tax_code"))
            .select_from(p.outerjoin(tn, tn.c.tenant_id == p.c.tenant_id).outerjoin(c, c.c.id == tn.c.customer_id))
            .order_by(p.c.created_at.desc(), p.c.id.desc()).limit(limit)).all()
    return [dict(r._mapping) for r in rows]


def payments_of(admin, tenant_id: str, limit: int = 60) -> list[dict]:
    """A newsroom's payments and refunds, newest first (what its director
    sees in the plan page)."""
    p = m.payments
    with accounts._read(admin) as conn:
        rows = conn.execute(select(p).where(p.c.tenant_id == tenant_id)
                            .order_by(p.c.created_at.desc(), p.c.id.desc()).limit(limit)).all()
    return [dict(r._mapping) for r in rows]


def month_numbers(payments: list[dict], subs: dict, now: datetime | None = None) -> dict:
    """The month so far: cents paid and refunded, payments that failed, and
    what the active plans bring in a month at list price."""
    now = _now(now)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    paid = refunded = failed = 0
    for x in payments:
        when = x.get("paid_at") or x.get("created_at")
        if when is None or (when.replace(tzinfo=UTC) if when.tzinfo is None else when) < start:
            continue
        if x["kind"] == "refund":
            refunded += x["amount_cents"] if x["status"] not in ("failed", "canceled") else 0
        elif x["status"] == "paid":
            paid += x["amount_cents"]
        elif x["status"] in ("failed", "expired", "canceled"):
            failed += 1
    return {"paid": paid, "refunded": refunded, "net": paid - refunded, "failed": failed,
            "mrr": sum(s.get("monthly_cents") or 0 for s in subs.values()),
            "paying": sum(1 for s in subs.values() if s["status"] == "active")}


def payment_tenant(admin, payment_row_id: int) -> str:
    """The newsroom a payment belongs to ("" when there is no such payment)."""
    with accounts._read(admin) as conn:
        return conn.execute(select(m.payments.c.tenant_id).where(m.payments.c.id == payment_row_id)).scalar() or ""


def refund(admin, gateway, payment_row_id: int, amount_cents: int | None, by: str, then: str = "none",
           now: datetime | None = None) -> dict:
    """Give back a payment made online, all of it (no amount) or part, and
    then nothing more, no more renewals, or stop the newsroom now. The
    gateway says how much is still refundable: a payment cannot be given
    back twice."""
    now = _now(now)
    if then not in REFUND_THEN:
        raise BillingError(t("scelta non valida dopo il rimborso"))
    if gateway is None:
        raise BillingError(t("il pagamento online non è configurato"))
    with accounts._read(admin) as conn:
        row = conn.execute(select(m.payments).where(m.payments.c.id == payment_row_id)).first()
    if row is None:
        raise LookupError(payment_row_id)
    if row.provider != gateway.name or row.status != "paid" or row.kind not in ("first", "recurring") \
            or not row.provider_payment_id:
        raise BillingError(t("si rimborsano solo i pagamenti online andati a buon fine"))
    pay = gateway.payment(row.provider_payment_id)
    if pay is None:
        raise BillingError(t("il pagamento non si trova su Mollie"))
    left = pay.get("remaining_cents")
    if left is None:
        left = max(0, pay["amount_cents"] - (pay.get("refunded_cents") or 0))
    amount = left if amount_cents is None else amount_cents
    if amount <= 0 or amount > left:
        raise BillingError(t("si può rimborsare al massimo {amount}", amount=money(left, row.currency)))
    try:
        rid, rstatus = gateway.refund(row.provider_payment_id, amount, row.currency,
                                      f"Redazia: rimborso {row.provider_payment_id}")
    except Exception as e:
        log.exception("refund of %s failed", row.provider_payment_id)
        raise BillingError(t("Mollie non ha accettato il rimborso: {error}", error=e)) from e
    with admin.engine.begin() as conn:
        conn.execute(insert(m.payments).values(
            tenant_id=row.tenant_id, subscription_id=row.subscription_id, provider=gateway.name,
            provider_payment_id=rid, kind="refund", amount_cents=amount, currency=row.currency, status=rstatus,
            note=f"{by}: rimborso di {row.provider_payment_id}"[:500], created_at=now, paid_at=now))
    log.info("refund %s of %s (%s cents) by %s, then %s", rid, row.provider_payment_id, amount, by, then)
    sub = subscription(admin, row.tenant_id)
    if then in ("renewals", "now") and sub is not None and sub["status"] != "canceled":
        cancel(admin, row.tenant_id, gateway, now)
    if then == "now" and sub is not None:
        with admin.engine.begin() as conn:
            conn.execute(update(m.subscriptions).where(m.subscriptions.c.tenant_id == row.tenant_id)
                         .values(status="expired", current_period_end=now, updated_at=now))
            _hold(conn, row.tenant_id)
    return {"refund_id": rid, "status": rstatus, "amount_cents": amount, "currency": row.currency,
            "tenant_id": row.tenant_id, "payment_id": row.provider_payment_id}


REFUND_OPEN = ("queued", "pending", "processing")


def sync_refunds(admin, gateway, tenant_id: str | None = None) -> int:
    """Read again from the gateway the refunds not finished yet: one
    cancelled in Mollie's dashboard (for example for lack of funds) shows
    as cancelled here too. Returns how many changed."""
    if gateway is None or not hasattr(gateway, "refund_status"):
        return 0
    p = m.payments
    q = select(p.c.id, p.c.provider_payment_id, p.c.note, p.c.status).where(
        p.c.kind == "refund", p.c.provider == gateway.name, p.c.status.in_(REFUND_OPEN))
    if tenant_id is not None:
        q = q.where(p.c.tenant_id == tenant_id)
    with accounts._read(admin) as conn:
        rows = conn.execute(q).all()
    changed = 0
    for r in rows:
        of = re.search(r"tr_\w+", r.note or "")
        if not of or not r.provider_payment_id:
            continue
        try:
            status = gateway.refund_status(of.group(0), r.provider_payment_id)
        except Exception:
            log.exception("refund %s not read from the gateway", r.provider_payment_id)
            continue
        if status and status != r.status:
            with admin.engine.begin() as conn:
                conn.execute(update(p).where(p.c.id == r.id).values(status=status))
            changed += 1
    return changed


def payment_by_provider_id(admin, payment_id: str) -> dict | None:
    with accounts._read(admin) as conn:
        row = conn.execute(select(m.payments).where(m.payments.c.provider_payment_id == payment_id)).first()
    return dict(row._mapping) if row else None


def paid_until_date(value: str | None) -> datetime | None:
    """A date typed in the console (YYYY-MM-DD): the end of that day, UTC."""
    if not value:
        return None
    d = date.fromisoformat(value.strip())
    return datetime(d.year, d.month, d.day, 23, 59, tzinfo=UTC)
