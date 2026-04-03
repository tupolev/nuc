import base64
import getpass
import hashlib
import hmac
import os
import sqlite3
from typing import Optional


API_KEY_SECRET_ENV = "API_KEY_SECRET"
API_KEY_SALT_ENV = "API_KEY_SALT"


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def derive_api_key_hash(api_key: str) -> str:
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")

    secret = get_required_env(API_KEY_SECRET_ENV).encode("utf-8")
    salt = get_required_env(API_KEY_SALT_ENV).encode("utf-8")
    material = salt + api_key.encode("utf-8")
    digest = hmac.new(secret, material, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def ensure_api_keys_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    rows = cur.execute("PRAGMA table_info(api_keys)").fetchall()
    columns = {row[1] for row in rows}

    if not rows:
        cur.execute(
            """
            CREATE TABLE api_keys (
                key_hash TEXT PRIMARY KEY,
                priority TEXT NOT NULL
            )
            """
        )
    elif "key_hash" in columns:
        if "priority" not in columns:
            cur.execute("ALTER TABLE api_keys ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'")
    conn.commit()


def has_legacy_plaintext_key_column(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    rows = cur.execute("PRAGMA table_info(api_keys)").fetchall()
    columns = {row[1] for row in rows}
    return "key" in columns and "key_hash" not in columns


def migrate_legacy_plaintext_keys(conn: sqlite3.Connection) -> int:
    if not has_legacy_plaintext_key_column(conn):
        return 0

    cur = conn.cursor()
    rows = cur.execute("SELECT key, priority FROM api_keys").fetchall()

    cur.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy")
    ensure_api_keys_schema(conn)

    migrated = 0
    for plaintext_key, priority in rows:
        key_hash = derive_api_key_hash(str(plaintext_key))
        cur.execute(
            """
            INSERT OR REPLACE INTO api_keys (key_hash, priority)
            VALUES (?, ?)
            """,
            (key_hash, priority),
        )
        migrated += 1

    cur.execute("DROP TABLE api_keys_legacy")
    conn.commit()
    return migrated


def prompt_new_api_key() -> str:
    while True:
        api_key = getpass.getpass("API key: ").strip()
        if not api_key:
            print("API key cannot be empty.")
            continue

        confirm = getpass.getpass("Repeat API key: ").strip()
        if api_key != confirm:
            print("API keys do not match. Please try again.")
            continue

        return api_key


def normalize_priority(value: Optional[str]) -> str:
    priority = (value or "").strip().lower()
    if priority in {"high", "medium", "low"}:
        return priority
    raise ValueError("priority must be one of: high, medium, low")
