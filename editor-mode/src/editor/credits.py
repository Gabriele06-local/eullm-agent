"""Credits: what goes beyond the plan, paid without surprises.

The plan gives a newsroom credits every month (`tenants.credits_month`,
copied from the plan). They pay for what the plan does not include:

- an article written after the month's articles of the plan are used up;
- a translation after the same quota (translations count as articles);
- a cover drawn again after the free ones: every article has its cover and
  FREE_REDRAWS new ones included, then each costs the image price.

The month's credits are used first and do not carry over; packs bought in
the Piano page (Mollie) or recorded by the operator (bank transfer, a gift,
a correction) add "extra" credits that do not expire. Every movement is a
row of `credit_ledger`. Prices are in `credit_prices` and the packs in
`credit_packs`, both changed from the console.

A newsroom whose `credits_month` is null does not use credits (the local
installation, or a newsroom without a plan): nothing is charged or stopped.

The owners get an email when 80% and then 100% of the month's credits are
used (once a month each), from the worker's hourly round.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import delete, func, insert, select, update

from . import i18n, models as m
from .i18n import t
from .quotas import QuotaExceeded, _zone, lock

log = logging.getLogger(__name__)

ACTIONS = ("article", "translation", "image")
DEFAULT_PRICES = {"article": 100, "translation": 10, "image": 5}  # 2026-11-08: 1 credit is about 1 cent
FREE_REDRAWS = 2  # covers drawn again for free, after the first one
ALERTS = (80, 100)


class NoCredits(QuotaExceeded):
    """Not enough credits for what goes beyond the plan."""


class CreditsError(ValueError):
    pass


def _now(now):
    return now or datetime.now(UTC)


def _mine(s, col):
    """A column of the session's own tenant row (the archive's row is
    readable too, so never the first row of the table)."""
    return s.execute(select(col).where(m.tenants.c.tenant_id == s.info["tenant_id"])).scalar()


def month_start(s, now: datetime | None = None) -> datetime:
    tz = _mine(s, m.tenants.c.timezone)
    local = _now(now).astimezone(_zone(tz))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def prices(conn) -> dict[str, int]:
    rows = dict(conn.execute(select(m.credit_prices.c.action, m.credit_prices.c.credits)).all())
    return {a: int(rows.get(a, DEFAULT_PRICES[a])) for a in ACTIONS}


def price(conn, action: str) -> int:
    return prices(conn)[action]


def state(s, now: datetime | None = None) -> dict:
    """The newsroom's credits now: the month's (given, used, left), the
    extra balance, and the share of the month's used."""
    month = _mine(s, m.tenants.c.credits_month)
    if month is None:
        return {"enabled": False}
    start = month_start(s, now)
    c = m.credit_ledger.c
    used = -int(s.execute(select(func.coalesce(func.sum(c.delta), 0))
                          .where(c.tenant_id == s.info["tenant_id"], c.pool == "month", c.at >= start, c.delta < 0)).scalar() or 0)
    extra = int(s.execute(select(func.coalesce(func.sum(c.delta), 0)).where(
        c.tenant_id == s.info["tenant_id"], c.pool == "extra")).scalar() or 0)
    left = max(0, month - used)
    return {"enabled": True, "month": month, "month_used": used, "month_left": left, "extra": max(0, extra),
            "left": left + max(0, extra), "pct": min(100, round(100 * used / month)) if month else 0,
            "since": start}


def need(s, credits: int) -> None:
    """Raise NoCredits when the newsroom cannot pay `credits` now."""
    st = state(s)
    if st["enabled"] and credits > st["left"]:
        raise NoCredits(f"not enough credits: {credits} needed, {st['left']} left")


def spend(s, action: str, ref: str | None = None, credits: int | None = None, note: str = "",
          now: datetime | None = None) -> int:
    """Pay `action` (its price, or `credits`): the month's credits first,
    then the extra ones. Holds the newsroom's quota lock until the
    transaction ends; do it with the insert it pays for. Returns the
    credits spent (0 when credits are not in use)."""
    tid = s.info["tenant_id"]
    lock(s, f"editor-quota:{tid}")
    st = state(s, now)
    if not st["enabled"]:
        return 0
    cost = price(s, action) if credits is None else credits
    if cost > st["left"]:
        raise NoCredits(f"not enough credits: {cost} needed, {st['left']} left")
    from_month = min(cost, st["month_left"])
    rows = []
    if from_month:
        rows.append({"pool": "month", "delta": -from_month})
    if cost - from_month:
        rows.append({"pool": "extra", "delta": -(cost - from_month)})
    for r in rows:
        s.execute(insert(m.credit_ledger).values(tenant_id=tid, at=_now(now), reason=action, ref=ref, note=note, **r))
    return cost


# --- covers ---------------------------------------------------------------------------


def covers_drawn(s, draft_id: int) -> int:
    c = m.credit_ledger.c
    return int(s.execute(select(func.count()).select_from(m.credit_ledger).where(
        c.tenant_id == s.info["tenant_id"], c.reason == "image", c.ref == f"draft:{draft_id}")).scalar() or 0)


def cover_cost(s, draft_id: int) -> int:
    """What the next cover of this draft costs: 0 for the first one and the
    free redraws, then the image price. 0 when credits are not in use."""
    if _mine(s, m.tenants.c.credits_month) is None:
        return 0
    return 0 if covers_drawn(s, draft_id) <= FREE_REDRAWS else price(s, "image")


def free_covers_left(s, draft_id: int) -> int:
    return max(0, FREE_REDRAWS + 1 - covers_drawn(s, draft_id))


def charge_cover(s, draft_id: int, now: datetime | None = None) -> int:
    """Count a cover of the draft, paying it when the free ones are used."""
    tid = s.info["tenant_id"]
    lock(s, f"editor-quota:{tid}")
    if _mine(s, m.tenants.c.credits_month) is None:
        return 0
    cost = cover_cost(s, draft_id)
    if cost:
        return spend(s, "image", f"draft:{draft_id}", now=now)
    s.execute(insert(m.credit_ledger).values(tenant_id=tid, at=_now(now), pool="month", delta=0, reason="image",
                                             ref=f"draft:{draft_id}", note="free"))
    return 0


# --- what the operator and payments add -----------------------------------------------


def grant(admin, tenant_id: str, credits: int, reason: str = "grant", note: str = "", by: str | None = None,
          ref: str | None = None) -> int:
    """Add extra credits (negative to correct). With a `ref` already
    recorded nothing is added twice (a payment notice delivered again)."""
    if not isinstance(credits, int) or isinstance(credits, bool) or credits == 0:
        raise CreditsError(t("i crediti sono un numero intero diverso da zero"))
    with admin.tenant(tenant_id) as s:
        if s.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.tenant_id == tenant_id)).first() is None:
            raise LookupError(tenant_id)
        c = m.credit_ledger.c
        if ref and s.execute(select(c.id).where(c.tenant_id == tenant_id, c.reason == reason, c.ref == ref)).first() is not None:
            return 0
        if credits < 0:
            lock(s, f"editor-quota:{tenant_id}")
            have = int(s.execute(select(func.coalesce(func.sum(c.delta), 0)).where(
                c.tenant_id == tenant_id, c.pool == "extra")).scalar() or 0)
            if have + credits < 0:
                raise CreditsError(t("si possono togliere al massimo {n} crediti extra", n=max(0, have)))
        s.execute(insert(m.credit_ledger).values(tenant_id=tenant_id, at=datetime.now(UTC), pool="extra",
                                                 delta=credits, reason=reason, ref=ref, note=note[:200], by_user=by))
    return credits


def ledger(s, limit: int = 20) -> list[dict]:
    """The latest movements, free covers left out."""
    c = m.credit_ledger.c
    rows = s.execute(select(m.credit_ledger).where(c.tenant_id == s.info["tenant_id"], c.delta != 0).order_by(c.at.desc(), c.id.desc()).limit(limit)).all()
    return [dict(r._mapping) for r in rows]


def balances(admin) -> dict[str, dict]:
    """Every newsroom's credits (the console)."""
    out = {}
    with admin.engine.connect() as conn:
        rooms = conn.execute(select(m.tenants.c.tenant_id).where(m.tenants.c.credits_month.is_not(None))).scalars().all()
    for tid in rooms:
        with admin.tenant(tid) as s:
            out[tid] = state(s)
    return out


# --- the catalogue (console) ------------------------------------------------------------


def set_prices(admin, values: dict) -> None:
    with admin.engine.begin() as conn:
        for action in ACTIONS:
            if action not in values:
                continue
            v = values[action]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise CreditsError(t("{field}: un numero di zero o più", field=action))
            if not conn.execute(update(m.credit_prices).where(m.credit_prices.c.action == action)
                                .values(credits=v)).rowcount:
                conn.execute(insert(m.credit_prices).values(action=action, credits=v))


def packs(admin, active_only: bool = False, s=None) -> list[dict]:
    """The packs; with `s`, read in that session (a newsroom's page)."""
    p = m.credit_packs
    q = select(p).order_by(p.c.price_cents, p.c.sort, p.c.code)  # cheapest first
    if active_only:
        q = q.where(p.c.active)
    if s is not None:
        return [dict(r._mapping) for r in s.execute(q).all()]
    with admin.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q).all()]


def get_pack(conn, code: str):
    pack = conn.execute(select(m.credit_packs).where(m.credit_packs.c.code == code)).first()
    if pack is None:
        raise CreditsError(t("pacchetto sconosciuto: {code}", code=code))
    return pack


def _check_pack(name, credits, price_cents) -> None:
    if not str(name or "").strip():
        raise CreditsError(t("il pacchetto ha bisogno di un nome"))
    if not isinstance(credits, int) or credits <= 0:
        raise CreditsError(t("i crediti del pacchetto sono un numero maggiore di zero"))
    if not isinstance(price_cents, int) or price_cents < 0:
        raise CreditsError(t("il prezzo è un numero di zero o più"))


def save_pack(admin, code: str, name: str, credits: int, price_cents: int, active: bool, sort: int = 0) -> None:
    _check_pack(name, credits, price_cents)
    with admin.engine.begin() as conn:
        if not conn.execute(update(m.credit_packs).where(m.credit_packs.c.code == code).values(
                name=name.strip()[:100], credits=credits, price_cents=price_cents, active=bool(active),
                sort=sort)).rowcount:
            raise CreditsError(t("pacchetto sconosciuto: {code}", code=code))


def create_pack(admin, code: str, name: str, credits: int, price_cents: int) -> str:
    code = (code or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", code):
        raise CreditsError(t("il codice è fatto di lettere minuscole, numeri e trattini (es. pro-plus)"))
    _check_pack(name, credits, price_cents)
    with admin.engine.begin() as conn:
        if conn.execute(select(m.credit_packs.c.code).where(m.credit_packs.c.code == code)).first() is not None:
            raise CreditsError(t("il pacchetto {code} esiste già", code=code))
        last = conn.execute(select(func.max(m.credit_packs.c.sort))).scalar() or 0
        conn.execute(insert(m.credit_packs).values(code=code, name=name.strip()[:100], credits=credits,
                                                   price_cents=price_cents, currency="EUR", active=True,
                                                   sort=last + 1))
    return code


def delete_pack(admin, code: str) -> str:
    """Remove a pack from sale. Credits already bought stay."""
    with admin.engine.begin() as conn:
        pack = conn.execute(select(m.credit_packs).where(m.credit_packs.c.code == code)).first()
        if pack is None:
            raise LookupError(code)
        conn.execute(delete(m.credit_packs).where(m.credit_packs.c.code == code))
    return pack.name


# --- alerts ---------------------------------------------------------------------------


def alerts(admin, mail=None, public_url: str = "", now: datetime | None = None) -> list[tuple[str, int]]:
    """Run every hour by the worker: tell the owners when 80% and 100% of
    the month's credits are used, once a month each. Returns what was sent."""
    from .billing import owners

    sent = []
    tn = m.tenants
    with admin.engine.connect() as conn:
        rooms = conn.execute(select(tn.c.tenant_id, tn.c.name, tn.c.language, tn.c.credits_alert)
                             .where(tn.c.credits_month > 0)).all()
    link = f"{public_url.rstrip('/')}/plan" if public_url else "/plan"
    for r in rooms:
        with admin.tenant(r.tenant_id) as s:
            st = state(s, now)
        if not st["enabled"]:
            continue
        month = f"{st['since']:%Y-%m}"
        reached = [a for a in ALERTS if st["pct"] >= a]
        if not reached:
            continue
        level = max(reached)
        last = r.credits_alert or ""
        if last.startswith(month + ":") and int(last.split(":")[1]) >= level:
            continue
        due = [a for a in reached
               if not (last.startswith(month + ":") and int(last.split(":")[1]) >= a)]
        with admin.engine.begin() as conn:
            conn.execute(update(tn).where(tn.c.tenant_id == r.tenant_id).values(credits_alert=f"{month}:{level}"))
        sent.extend((r.tenant_id, a) for a in due)
        if mail is None:
            continue
        lang = r.language if r.language in i18n.LANGS else i18n.DEFAULT
        for level in due:
            if level >= 100:
                subject = t("{name}: crediti del mese finiti", lang, name=r.name)
                body = t("La redazione {name} ha usato tutti i {n} crediti di questo mese.", lang, name=r.name,
                         n=st["month"])
                body += " " + (t("Restano {n} crediti extra, usati da adesso.", lang, n=st["extra"]) if st["extra"]
                               else t("Gli articoli del piano continuano; quello che va oltre il piano si ferma finché "
                                      "non aggiungi crediti o non riparte il mese.", lang))
            else:
                subject = t("{name}: usato l'80% dei crediti del mese", lang, name=r.name)
                body = t("La redazione {name} ha usato {used} dei {n} crediti di questo mese.", lang, name=r.name,
                         used=st["month_used"], n=st["month"])
            body += "\n\n" + t("Crediti e pacchetti nella pagina Il mio piano:", lang) + f"\n\n{link}\n"
            for to in owners(admin, r.tenant_id):
                try:
                    mail(to, subject, body)
                except Exception:
                    log.exception("credits alert to %s failed", to)
    return sent
