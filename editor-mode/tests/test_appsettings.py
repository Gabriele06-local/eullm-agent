"""The settings of /admin › Impostazioni (appsettings.py): stored encrypted
when secret, never shown again, the .env file always wins, the services read
them at start, a change restarts the worker once its jobs are done."""

import base64
import json
import os
import threading

import pytest
from sqlalchemy import delete, select
from test_console_sales import operator

from editor import appsettings, queue
from editor import models as m
from editor.config import Settings
from editor.db import Database

KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture
def admin(db, monkeypatch):
    for f in appsettings.FIELDS:
        monkeypatch.delenv(f.name, raising=False)
    monkeypatch.setenv("EDITOR_SECRET_KEY", KEY)
    monkeypatch.setattr(appsettings, "_real", None)
    saved = dict(os.environ)  # `load` writes into os.environ: the next tests get it back as it was
    a = Database.from_url(db.admin_url)
    with a.engine.begin() as conn:  # the tests share the database: a fresh installation
        conn.execute(delete(m.app_settings))
    yield a
    with a.engine.begin() as conn:
        conn.execute(delete(m.app_settings))
    a.engine.dispose()
    appsettings._real = None
    os.environ.clear()
    os.environ.update(saved)


def gsc_key() -> str:
    return json.dumps({"type": "service_account", "client_email": "redazia@progetto.iam.gserviceaccount.com",
                       "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"})


def test_secrets_are_stored_encrypted_and_shown_only_by_their_last_characters(admin):
    changed = appsettings.save(admin, {"EDITOR_SMTP_HOST": "mail.redazia.com", "EDITOR_SMTP_PASSWORD": "segreta-1234",
                                       "EDITOR_MOLLIE_API_KEY": "test_abcdefgh5678", "EDITOR_GSC_KEY": gsc_key()},
                               set(), "op@example.com")
    assert set(changed) == {"EDITOR_SMTP_HOST", "EDITOR_SMTP_PASSWORD", "EDITOR_MOLLIE_API_KEY", "EDITOR_GSC_KEY"}
    with admin.engine.connect() as conn:
        rows = {r.name: r for r in conn.execute(select(m.app_settings)).all()}
    assert rows["EDITOR_SMTP_HOST"].value == "mail.redazia.com" and not rows["EDITOR_SMTP_HOST"].secret
    assert "segreta" not in rows["EDITOR_SMTP_PASSWORD"].value and rows["EDITOR_SMTP_PASSWORD"].secret
    assert rows["EDITOR_SMTP_PASSWORD"].updated_by == "op@example.com"
    values, broken = appsettings.stored(admin)
    assert values["EDITOR_SMTP_PASSWORD"] == "segreta-1234" and not broken
    items = {i["f"].name: i for g in appsettings.view(admin)["groups"] for i in g["items"]}
    assert items["EDITOR_SMTP_PASSWORD"]["shown"] == "••••1234" and items["EDITOR_SMTP_PASSWORD"]["value"] == ""
    assert items["EDITOR_MOLLIE_API_KEY"]["source"] == "panel"
    assert items["EDITOR_GSC_KEY"]["account"] == "redazia@progetto.iam.gserviceaccount.com"
    assert items["EDITOR_TIMEZONE"]["source"] == "default"

    # An empty secret keeps the stored one; an emptied plain value goes back to the default.
    assert appsettings.save(admin, {"EDITOR_SMTP_PASSWORD": "", "EDITOR_SMTP_HOST": ""}, set(), "op") == ["EDITOR_SMTP_HOST"]
    values, _ = appsettings.stored(admin)
    assert values["EDITOR_SMTP_PASSWORD"] == "segreta-1234" and "EDITOR_SMTP_HOST" not in values
    # A secret is deleted only on purpose.
    appsettings.save(admin, {}, {"EDITOR_SMTP_PASSWORD"}, "op")
    assert "EDITOR_SMTP_PASSWORD" not in appsettings.stored(admin)[0]


def test_a_secret_stored_with_another_key_asks_to_be_entered_again(admin, monkeypatch):
    appsettings.save(admin, {"EDITOR_TELEGRAM_TOKEN": "123:abcdefghij"}, set(), "op")
    monkeypatch.setenv("EDITOR_SECRET_KEY", base64.b64encode(b"z" * 32).decode())
    values, broken = appsettings.stored(admin)
    assert "EDITOR_TELEGRAM_TOKEN" not in values and broken == {"EDITOR_TELEGRAM_TOKEN": "otherkey"}


def test_the_env_file_wins_unless_it_holds_the_default(admin, monkeypatch):
    monkeypatch.setenv("EDITOR_SMTP_HOST", "smtp.env.example")
    monkeypatch.setenv("EDITOR_TIMEZONE", "Europe/Rome")  # what older compose files pass anyway
    monkeypatch.setattr(appsettings, "_real", None)
    appsettings.save(admin, {"EDITOR_SMTP_HOST": "mail.panel.example", "EDITOR_TIMEZONE": "Europe/London"}, set(), "op")
    values, _ = appsettings.stored(admin)
    assert "EDITOR_SMTP_HOST" not in values and values["EDITOR_TIMEZONE"] == "Europe/London"
    items = {i["f"].name: i for g in appsettings.view(admin)["groups"] for i in g["items"]}
    assert items["EDITOR_SMTP_HOST"]["source"] == "env" and items["EDITOR_SMTP_HOST"]["shown"] == "smtp.env.example"
    st = Settings.from_env(appsettings.effective(admin))
    assert st.smtp_host == "smtp.env.example" and st.timezone == "Europe/London"
    appsettings.load(admin)
    assert os.environ["EDITOR_SMTP_HOST"] == "smtp.env.example" and os.environ["EDITOR_TIMEZONE"] == "Europe/London"


def test_load_fills_the_environment_and_a_change_is_seen(admin, monkeypatch):
    appsettings.save(admin, {"EDITOR_RETENTION_DAYS": "90", "EDITOR_TURNSTILE_SECRET": "0x4AAAsegreto"}, set(), "op")
    appsettings.load(admin)
    from editor import readers, tenant_data
    assert tenant_data.retention_days() == 90 and readers.turnstile_keys()[1] == "0x4AAAsegreto"
    assert not appsettings.pending(admin)
    appsettings.save(admin, {"EDITOR_RETENTION_DAYS": "60"}, set(), "op")
    assert appsettings.pending(admin)


@pytest.mark.parametrize("name,value", [("EDITOR_SMTP_PORT", "porta"), ("EDITOR_TIMEZONE", "Europe/Atlantide"),
                                        ("EDITOR_MOLLIE_API_KEY", "sk_123"), ("EDITOR_PUBLIC_URL", "app.redazia.com"),
                                        ("EDITOR_GSC_KEY", "{}"), ("EDITOR_IMAGES_QUALITY", "ultra"),
                                        ("EDITOR_SMTP_HOST", "a\nb")])
def test_wrong_values_are_refused_with_their_field(admin, name, value):
    with pytest.raises(appsettings.Invalid) as e:
        appsettings.save(admin, {name: value}, set(), "op")
    assert e.value.label == appsettings.BY_NAME[name].label
    assert appsettings.stored(admin)[0] == {}


def test_an_empty_variable_is_the_default():
    st = Settings.from_env({"EDITOR_TIMEZONE": "", "EDITOR_SMTP_PORT": "", "EDITOR_IMAGES_QUALITY": ""})
    assert (st.timezone, st.smtp_port, st.images_quality) == ("Europe/Rome", 587, "medium")


def test_the_console_page_saves_and_the_tests_say_what_is_missing(db, admin):
    c = operator(db, admin)
    page = c.get("/admin/settings")
    assert page.status_code == 200 and "Impostazioni del server" in page.text and "EDITOR_SMTP_PASSWORD" in page.text
    r = c.post("/admin/settings", data={"EDITOR_SMTP_HOST": "mail.redazia.com", "EDITOR_SMTP_PASSWORD": "pw-9876",
                                        "EDITOR_SMTP_PORT": "587", "EDITOR_IMAGES_QUALITY": "medium"})
    assert r.status_code == 200 and "Impostazioni salvate" in r.text
    assert "••••" in r.text and "pw-9876" not in r.text and "salvata qui" in r.text
    r = c.post("/admin/settings", data={"EDITOR_TIMEZONE": "Marte/Base"})
    assert "Fuso orario: fuso orario sconosciuto" in r.text
    assert "Manca la chiave di Mollie" in c.post("/admin/settings/test-mollie").text
    assert "Manca il token del bot" in c.post("/admin/settings/test-telegram").text


def test_a_draining_worker_finishes_its_job_then_exits(db, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    calls = []

    def slow(*_):
        started.set()
        finish.wait(5)
        calls.append("done")

    monkeypatch.setattr(queue, "claim", lambda *_: (None if calls or started.is_set() else
                                                     queue.Job(1, "t", "slow", "manual", {}, 1)))
    monkeypatch.setattr(queue, "run", lambda d, job, h: slow())
    monkeypatch.setattr(queue, "requeue_stale", lambda *a, **k: 0)
    monkeypatch.setattr(queue, "enqueue_ticks", lambda *a, **k: 0)
    monkeypatch.setattr(queue.signal, "signal", lambda *a: None)
    w = queue.Worker(db, {}, concurrency=1)
    th = threading.Thread(target=w.run)
    th.start()
    assert started.wait(5)
    w.drain()
    th.join(1.5)
    assert th.is_alive()  # still on its job
    finish.set()
    th.join(5)
    assert not th.is_alive() and calls == ["done"]
