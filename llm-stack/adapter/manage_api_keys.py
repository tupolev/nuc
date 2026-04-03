#!/usr/bin/env python3

import argparse
import os
import sqlite3

from auth_security import (
    derive_api_key_hash,
    ensure_api_keys_schema,
    migrate_legacy_plaintext_keys,
    normalize_priority,
    prompt_new_api_key,
)


AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "/data/auth.db")


def connect_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(AUTH_DB_PATH)
    return conn


def create_key(conn: sqlite3.Connection, priority: str) -> None:
    api_key = prompt_new_api_key()
    key_hash = derive_api_key_hash(api_key)

    conn.execute(
        """
        INSERT OR REPLACE INTO api_keys (key_hash, priority)
        VALUES (?, ?)
        """,
        (key_hash, priority),
    )
    conn.commit()
    print("API key stored successfully in protected format.")
    print(f"priority={priority}")
    print(f"key_hash={key_hash}")


def list_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT key_hash, priority FROM api_keys ORDER BY priority, key_hash"
    ).fetchall()

    if not rows:
        print("No stored API keys found.")
        return

    for key_hash, priority in rows:
        print(f"{priority}\t{key_hash}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage protected API keys for the adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new API key")
    create_parser.add_argument(
        "--priority",
        default="medium",
        choices=["high", "medium", "low"],
        help="Priority for the new API key",
    )

    subparsers.add_parser("list", help="List stored protected API keys")
    subparsers.add_parser("migrate-legacy", help="Migrate plaintext legacy API keys to protected storage")

    args = parser.parse_args()

    conn = connect_db()
    ensure_api_keys_schema(conn)

    if args.command == "migrate-legacy":
        migrated = migrate_legacy_plaintext_keys(conn)
        print(f"Legacy rows migrated: {migrated}")
        return 0

    if args.command == "list":
        migrated = migrate_legacy_plaintext_keys(conn)
        if migrated:
            print(f"Legacy rows migrated: {migrated}")
        list_keys(conn)
        return 0

    if args.command == "create":
        migrated = migrate_legacy_plaintext_keys(conn)
        if migrated:
            print(f"Legacy rows migrated: {migrated}")
        create_key(conn, normalize_priority(args.priority))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
