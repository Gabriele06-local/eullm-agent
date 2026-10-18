"""Secrets of the tenants (WordPress application passwords, webhook signing
keys, Telegram bot tokens), kept encrypted in the database so an owner can
enter them from the dashboard instead of asking the operator for an
environment variable.

AES-256-GCM, with a key that never enters the database:
- on a server, EDITOR_SECRET_KEY (32 random bytes in base64; `editor
  secret-key` prints a new one);
- on the local installation (SQLite), if that variable is not set, a key
  file next to the database, created on first use and readable by its
  owner only.
The tenant and the secret's name are the associated data, so a ciphertext
copied to another tenant or another name does not decrypt. Every row
carries the id of the key that wrote it: a server started with another key
says so instead of failing with a decryption error. A stored value is never
shown again; it can only be replaced or deleted.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, insert, select

from . import models as m

KEY_ENV = "EDITOR_SECRET_KEY"
KEY_FILE = "editor-secret.key"
NONCE_BYTES = 12


class VaultError(RuntimeError):
    pass


def new_key() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


def _decode(text: str) -> bytes:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise VaultError(f"{KEY_ENV} must be 32 bytes in base64 (`editor secret-key` prints one)")
    return key


def key_for(db) -> bytes | None:
    """The key, or None when this installation has none (secrets can then
    come only from environment variables)."""
    if os.environ.get(KEY_ENV):
        return _decode(os.environ[KEY_ENV])
    if not db.sqlite:
        return None
    # The engine attaches the file as schema `editor`: its path is there.
    with db.engine.connect() as conn:
        files = {row[1]: row[2] for row in conn.exec_driver_sql("PRAGMA database_list").all()}
    if not files.get("editor"):
        raise VaultError("cannot find the database file to keep the key next to it")
    keyfile = Path(files["editor"]).parent / KEY_FILE
    if not keyfile.exists():
        fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(new_key() + "\n")
    return _decode(keyfile.read_text())


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"editor-vault\0" + key).hexdigest()[:12]


def _aad(tenant_id: str, name: str) -> bytes:
    return f"{tenant_id}\0{name}".encode()


def put(db, tenant_id: str, name: str, value: str) -> None:
    from .publishing import SECRET_NAME

    if not SECRET_NAME.match(name or "") or len(name) > 40:
        raise VaultError("the secret name must be UPPERCASE letters and digits, words joined by single underscores")
    if not value or len(value) > 4096:
        raise VaultError("the secret is empty or longer than 4096 characters")
    key = key_for(db)
    if key is None:
        raise VaultError(f"this server has no {KEY_ENV}: secrets cannot be stored, only read from the environment")
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, value.encode(), _aad(tenant_id, name))
    with db.tenant(tenant_id) as s:
        s.execute(delete(m.secrets).where(m.secrets.c.name == name))
        s.execute(insert(m.secrets).values(
            tenant_id=tenant_id, name=name, ciphertext=base64.b64encode(nonce + sealed).decode(),
            key_id=key_id(key), updated_at=datetime.now(UTC)))


def get(db, tenant_id: str, name: str) -> str | None:
    """The secret's value, or None when it is not stored."""
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.secrets.c.ciphertext, m.secrets.c.key_id).where(m.secrets.c.name == name)).first()
    if row is None:
        return None
    key = key_for(db)
    if key is None:
        raise VaultError(f"secret {name} is stored encrypted but this server has no {KEY_ENV}")
    if key_id(key) != row.key_id:
        raise VaultError(f"secret {name} was stored with another {KEY_ENV}: enter it again")
    raw = base64.b64decode(row.ciphertext)
    try:
        return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], _aad(tenant_id, name)).decode()
    except InvalidTag:
        raise VaultError(f"secret {name} does not decrypt: enter it again") from None


def remove(db, tenant_id: str, name: str) -> bool:
    with db.tenant(tenant_id) as s:
        return s.execute(delete(m.secrets).where(m.secrets.c.name == name)).rowcount > 0


def stored(s) -> set[str]:
    """Names of the secrets stored for the session's tenant."""
    return set(s.execute(select(m.secrets.c.name)).scalars())
