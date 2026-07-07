import json
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException

from token_reports import get_top_holders_report

# Registry of issued client API keys. Lives outside the repo in the runtime
# data dir (like tracked_wallets/servers data) so real keys are never committed.
API_CLIENTS_FILE = "/home/shaolin_saga/data/api_clients.json"

logger = logging.getLogger('api')

app = FastAPI(title="Shaolin Saga API")


def _load_api_keys() -> set:
    if not os.path.exists(API_CLIENTS_FILE):
        return set()
    with open(API_CLIENTS_FILE, 'r') as f:
        clients = json.load(f)
    return set(clients.keys())


async def verify_api_key(x_api_key: str = Header(...)) -> str:
    if x_api_key not in _load_api_keys():
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


@app.get("/v1/tokens/{token_address}/top-holders")
async def top_holders_endpoint(token_address: str, limit: int = 10, api_key: str = Depends(verify_api_key)):
    try:
        return await get_top_holders_report(token_address, logger, limit=limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid token address")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
