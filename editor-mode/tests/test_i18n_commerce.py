"""The public sales site signs people up in their own language: the errors
of the signup and the owner's invite follow `language` (or Accept-Language)."""

from fastapi.testclient import TestClient

from editor import accounts
from editor.api import create_app
from editor.db import Database

CUSTOMER = {"kind": "company", "name": "Rossi Media srl", "vat_number": "IT 01234567890", "tax_code": "01234567890",
            "address": "Via Roma 1", "postal_code": "00100", "city": "Roma", "province": "rm", "country": "it",
            "sdi_code": "abc1234"}


def unique(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _client(db):
    sent = []
    app = create_app(db, mail=lambda to, subject, body: sent.append((to, subject, body)),
                     public_url="https://editor.example.com")
    return TestClient(app), sent


def _key(db):
    token = accounts.create_operator_token(Database.from_url(db.admin_url), "sito")
    return {"Authorization": f"Bearer {token}"}


def test_signup_speaks_the_visitors_language(db):
    c, sent = _client(db)
    headers = _key(db)
    email = f"{unique('amm')}@rossi.it"
    body = {"customer": CUSTOMER | {"email": email, "vat_number": "123"}, "newsroom": "Rossi News",
            "external_ref": unique("web"), "language": "de"}
    r = c.post("/api/operator/signup", json=body, headers=headers)
    assert r.status_code == 400 and r.json()["error"] == "die italienische USt-IdNr. hat 11 Ziffern"
    # The header works too, and an unknown language is refused.
    r = c.post("/api/operator/signup", json=body | {"language": None}, headers=headers | {"Accept-Language": "fr"})
    assert r.json()["error"] == "le numéro de TVA italien a 11 chiffres"
    assert c.post("/api/operator/signup", json=body | {"language": "xx"}, headers=headers).status_code == 422
    # A good signup: the owner's invite in German.
    r = c.post("/api/operator/signup", json=body | {"customer": CUSTOMER | {"email": email}}, headers=headers)
    assert r.status_code == 201, r.text
    to, subject, text = sent[-1]
    assert to == email and subject == "Einladung in die Redaktion Rossi News"
    assert "hat Sie in die Redaktion Rossi News auf EuLLM Editor eingeladen" in text
    # The language was the request's only: the next one is Italian again (the tests' default).
    r = c.post("/api/operator/signup", json=body | {"language": None}, headers=headers)
    assert r.json()["error"] == "la partita IVA italiana ha 11 cifre"
