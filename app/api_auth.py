import hashlib
import json
import os

# Registry of issued client API keys. Lives outside the repo in the runtime
# data dir (like tracked_wallets/servers data) so real keys are never committed.
API_CLIENTS_FILE = "/home/shaolin_saga/data/api_clients.json"

KEY_PREFIX = "ss_live_"


def hash_key(key: str) -> str:
    """Keys are stored (and looked up) by hash, never in plaintext."""
    return hashlib.sha256(key.encode()).hexdigest()


def load_clients() -> dict:
    """Returns {key_hash: {client_name, created_at, key_preview}}."""
    if not os.path.exists(API_CLIENTS_FILE):
        return {}
    with open(API_CLIENTS_FILE, 'r') as f:
        return json.load(f)


def save_clients(clients: dict) -> None:
    os.makedirs(os.path.dirname(API_CLIENTS_FILE), exist_ok=True)
    with open(API_CLIENTS_FILE, 'w') as f:
        json.dump(clients, f, indent=2)
