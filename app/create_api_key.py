#!/usr/bin/env python3
"""Issue a new Shaolin Saga API key for a client.

Usage: python3 create_api_key.py "Client Name"

Run manually today; this is the function a self-service signup/payment flow
will call directly once one exists.
"""
import argparse
import secrets
from datetime import datetime, timezone

from api_auth import KEY_PREFIX, hash_key, load_clients, save_clients


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def create_api_key(client_name: str) -> str:
    key = generate_key()
    key_hash = hash_key(key)

    clients = load_clients()
    clients[key_hash] = {
        "client_name": client_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "key_preview": key[:len(KEY_PREFIX) + 6] + "...",
    }
    save_clients(clients)

    return key


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Issue a new Shaolin Saga API key")
    parser.add_argument("client_name", help="Name of the customer/client this key is for")
    args = parser.parse_args()

    new_key = create_api_key(args.client_name)

    print(f"API key created for '{args.client_name}':")
    print(new_key)
    print("\nStore this now - it will not be shown again.")
