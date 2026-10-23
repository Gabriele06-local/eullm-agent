"""Whatever a person creates from the dashboard can be corrected or deleted
from it: sites, publishing targets, briefing recipients, drafts and the
newsroom's preferences."""

import json
import socket

from sqlalchemy import text

from editor import queue, repo, vault
from test_publishing import approved_draft, public_resolver
from test_ui import logged_in


def count(db, tenant, table, where="TRUE", **params):
    with db.tenant(tenant) as s:
        return s.execute(text(f"SELECT count(*) FROM editor.{table} WHERE {where}"), params).scalar()


def test_owner_deletes_a_site_only_by_writing_its_domain(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(vault.KEY_ENV, vault.new_key())
    with db.tenant(tenant) as s:
        domain = s.execute(text("SELECT domain FROM editor.sites WHERE id = :i"), {"i": site_id}).scalar()
    queue.enqueue(db, tenant, "discovery", domain, {"site_id": site_id})
    owner = logged_in(db, tenant, "owner")
    owner.post("/ui/targets", data={"site_id": site_id, "kind": "webhook", "name": "Hook",
                                    "address": "https://hooks.example/in", "secret_env": "HOOK", "secret": "s3cret"})
    assert count(db, tenant, "publish_targets") == 1
    assert "Elimina il sito" in owner.get("/site").text

    editor = logged_in(db, tenant, "editor")
    assert editor.post(f"/ui/sites/{site_id}/delete", data={"confirm": domain}).status_code == 403
    page = owner.post(f"/ui/sites/{site_id}/delete", data={"confirm": "wrong.example"})
    assert "niente è stato eliminato" in page.text and count(db, tenant, "sites") == 1

    page = owner.post(f"/ui/sites/{site_id}/delete", data={"confirm": domain.upper()})
    assert f"{domain} eliminato" in page.text
    assert count(db, tenant, "sites") == 0
    for table in ("drafts", "proposals", "editorial_profiles", "publish_targets", "secrets"):
        assert count(db, tenant, table) == 0, table
    assert count(db, tenant, "jobs", "status = 'queued'") == 0
    assert owner.get("/").status_code == 200 and owner.get("/site").status_code == 200


def test_a_site_being_read_is_not_deleted_under_the_worker(db, tenant):
    _, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        domain = s.execute(text("SELECT domain FROM editor.sites WHERE id = :i"), {"i": site_id}).scalar()
    queue.enqueue(db, tenant, "analysis", domain)
    queue.claim(db, "w", tenant)
    owner = logged_in(db, tenant, "owner")
    page = owner.post(f"/ui/sites/{site_id}/delete", data={"confirm": domain})
    assert "lavoro in corso" in page.text and count(db, tenant, "sites") == 1


def test_a_mistyped_domain_is_corrected_until_the_profile_is_approved(db, tenant):
    with db.tenant(tenant) as s:
        wrong = repo.add_site(s, tenant, "exmaple.it")
    owner = logged_in(db, tenant, "owner")
    owner.get(f"/switch?site={wrong}")
    assert 'name="domain" value="exmaple.it"' in owner.get("/site").text
    page = owner.post(f"/ui/sites/{wrong}/edit", data={"name": "Esempio", "domain": "https://www.example.it/"})
    assert "Dominio corretto da exmaple.it a example.it" in page.text
    with db.tenant(tenant) as s:
        assert list(s.execute(text("SELECT domain FROM editor.sites")).scalars()) == ["example.it"]
    assert count(db, tenant, "jobs", "kind = 'analysis' AND subject = 'example.it' AND status = 'queued'") == 1

    _, site_id = approved_draft(db, tenant)
    page = owner.post(f"/ui/sites/{site_id}/edit", data={"name": "Il blog", "domain": "other.example"})
    assert "già approvato" in page.text
    page = owner.post(f"/ui/sites/{site_id}/edit", data={"name": "Il blog"})
    assert "Sito aggiornato." in page.text
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT name FROM editor.sites WHERE id = :i"), {"i": site_id}).scalar() == "Il blog"


def test_targets_are_changed_and_deleted_with_their_credential(db, tenant, monkeypatch):
    _, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(vault.KEY_ENV, vault.new_key())
    owner = logged_in(db, tenant, "owner")
    owner.post("/ui/targets", data={"site_id": site_id, "kind": "wordpress", "name": "Blog", "user": "red",
                                    "address": "https://blog.example", "secret_env": "WP_BLOG", "secret": "pw"})
    with db.tenant(tenant) as s:
        tid = s.execute(text("SELECT id FROM editor.publish_targets")).scalar()
    assert "Modifica o elimina Blog" in owner.get("/publications").text

    page = owner.post(f"/ui/targets/{tid}", data={"name": "Blog nuovo", "address": "http://blog.example", "user": "red"})
    assert "Destinazione non modificata" in page.text
    page = owner.post(f"/ui/targets/{tid}", data={"name": "Blog nuovo", "address": "https://www.blog.example",
                                                  "user": "redazione", "indexnow_key": "abcdef123", "jsonld": "1"})
    assert "Destinazione Blog nuovo aggiornata." in page.text
    with db.tenant(tenant) as s:
        row = s.execute(text("SELECT name, config, enabled FROM editor.publish_targets")).first()
    config = json.loads(row.config) if isinstance(row.config, str) else row.config
    assert row.name == "Blog nuovo" and not row.enabled
    assert config["url"] == "https://www.blog.example" and config["user"] == "redazione"
    assert config["indexnow_key"] == "abcdef123" and config["jsonld"] is True

    assert vault.get(db, tenant, "WP_BLOG") == "pw"
    page = owner.post(f"/ui/targets/{tid}/delete")
    assert "Destinazione Blog nuovo eliminata" in page.text
    assert count(db, tenant, "publish_targets") == 0 and vault.get(db, tenant, "WP_BLOG") is None


def test_recipients_and_preferences_are_changed_by_the_owner(db, tenant):
    owner = logged_in(db, tenant, "owner")
    owner.post("/ui/recipients", data={"channel": "email", "address": "a@example.it"})
    with db.tenant(tenant) as s:
        rid = s.execute(text("SELECT id FROM editor.recipients")).scalar()
    assert "Destinatario non valido" in owner.post(f"/ui/recipients/{rid}", data={"channel": "fax", "address": "x"}).text
    page = owner.post(f"/ui/recipients/{rid}", data={"channel": "email", "address": "b@example.it"})
    assert "Destinatario aggiornato." in page.text
    with db.tenant(tenant) as s:
        r = s.execute(text("SELECT address, enabled FROM editor.recipients")).first()
    assert r.address == "b@example.it" and not r.enabled

    assert "Fuso orario sconosciuto" in owner.post("/ui/preferences", data={
        "timezone": "Mars/Base", "briefing_hour": "7", "language": "de"}).text
    page = owner.post("/ui/preferences", data={"timezone": "Europe/Berlin", "briefing_hour": "7", "language": "de"})
    assert "Preferenze salvate" in page.text or "Einstellungen" in page.text
    with db.tenant(tenant) as s:
        t = s.execute(text("SELECT timezone, briefing_hour, language FROM editor.tenants")).first()
    assert (t.timezone, t.briefing_hour, t.language) == ("Europe/Berlin", 7, "de")
    editor = logged_in(db, tenant, "editor")
    assert editor.post("/ui/preferences", data={"timezone": "UTC", "briefing_hour": "1", "language": "it"}).status_code == 403


def test_a_draft_is_edited_checked_again_and_deleted(db, tenant):
    did, _ = approved_draft(db, tenant)
    editor = logged_in(db, tenant, "editor")
    form = editor.get(f"/drafts/{did}?edit=1").text
    assert 'name="claim-0"' in form and 'name="seo_keyphrase"' in form

    page = editor.post(f"/ui/drafts/{did}/edit", data={
        "title": "Bari cablata", "subtitle": "Fibra ovunque", "claim-0": "Open Fiber ha lavorato a Bari per 900 giorni.",
        "seo_keyphrase": "fibra Bari", "seo_title": "Fibra a Bari", "seo_description": "", "seo_slug": "fibra-bari"})
    assert "Modifiche salvate." in page.text and "va riapprovata" in page.text and "non passano i controlli" in page.text
    with db.tenant(tenant) as s:
        d = s.execute(text("SELECT title, subtitle, status, body_md, seo FROM editor.drafts WHERE id = :d"), {"d": did}).first()
        first = s.execute(text("SELECT text FROM editor.draft_claims WHERE draft_id = :d AND ordinal = 0"), {"d": did}).scalar()
    assert d.title == "Bari cablata" and d.subtitle == "Fibra ovunque" and d.status == "needs_review"
    assert "900 giorni" in first and "# Bari cablata" in d.body_md and json.loads(d.seo)["slug"] == "fibra-bari" if isinstance(d.seo, str) else d.seo["slug"] == "fibra-bari"

    assert "non salvate" in editor.post(f"/ui/drafts/{did}/edit", data={"title": "Bari", "claim-1": ""}).text

    page = editor.post(f"/ui/drafts/{did}/delete")
    assert "eliminata" in page.text and count(db, tenant, "drafts") == 0
    assert count(db, tenant, "proposals", "status = 'accepted'") == 1
    viewer = logged_in(db, tenant, "viewer")
    assert viewer.post("/ui/drafts/999/delete").status_code == 403


def test_the_console_changes_and_deletes_what_it_created(db):
    from editor import accounts, billing
    from editor.db import Database
    from test_billing import client, key, signup, unique

    admin = Database.from_url(db.admin_url)
    c, _ = client(db)
    op, other = f"{unique('op')}@example.com", f"{unique('op')}@example.com"
    me = accounts.create_user(admin, op, password="password operatore", operator=True)
    them = accounts.create_user(admin, other, password="password operatore", operator=True)
    tid = signup(c, key(db), unique("web"), f"{unique('amm')}@rossi.it").json()["tenant"]["tenant_id"]
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin").text
    assert "Nome, piano e limiti" in page and "Aggiungi un piano" in page and "Modifica o elimina Rossi Media srl" in page

    c.post(f"/admin/newsrooms/{tid}", data={"name": "Rossi Notizie", "plan": ""})
    assert next(r for r in billing.customers(admin) for r in r["newsrooms"] if r["tenant_id"] == tid)["name"] == "Rossi Notizie"

    cid = next(x["id"] for x in billing.customers(admin) if any(n["tenant_id"] == tid for n in x["newsrooms"]))
    r = c.post(f"/admin/customers/{cid}", data={"kind": "company", "name": "Rossi Media spa", "email": "a@rossi.it",
                                               "vat_number": "IT01234567890", "address": "Via Po 2", "postal_code": "00100",
                                               "city": "Roma", "country": "IT", "sdi_code": "ABC1234"})
    assert "Cliente Rossi Media spa aggiornato." in r.text
    assert billing.customer(admin, cid)["address"] == "Via Po 2"
    assert "ha ancora 1 redazioni" in c.post(f"/admin/customers/{cid}/delete").text

    code = unique("plan").lower()
    assert "Piano Extra creato" in c.post("/admin/plans", data={"code": code, "name": "Extra"}).text
    assert "non eliminato" in c.post("/admin/plans/trial/delete").text
    assert "Piano Extra eliminato." in c.post(f"/admin/plans/{code}/delete").text

    rid = billing.create_reseller(admin, "Agenzia Blu", f"{unique('blu')}@example.com")
    r = c.post(f"/admin/resellers/{rid}", data={"name": "Agenzia Azzurra", "email": "azzurra@example.com", "discount": "10"})
    assert "Rivenditore Agenzia Azzurra aggiornato." in r.text
    assert billing.reseller(admin, rid).discount_pct == 10
    assert "eliminato con le sue chiavi" in c.post(f"/admin/resellers/{rid}/delete").text
    assert billing.reseller(admin, rid) is None

    assert "non puoi togliere te stesso" in c.post(f"/admin/operators/{me}/remove").text
    assert "non è più operatore" in c.post(f"/admin/operators/{them}/remove").text
    assert not any(o.id == them for o in accounts.operators(admin))
    admin.engine.dispose()
