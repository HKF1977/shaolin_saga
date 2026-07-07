import logging

from fastapi import Depends, FastAPI, Header, HTTPException

from api_auth import hash_key, load_clients
from token_reports import get_top_holders_report

logger = logging.getLogger('api')

app = FastAPI(title="Shaolin Saga API")


async def verify_api_key(x_api_key: str = Header(...)) -> str:
    key_hash = hash_key(x_api_key)
    if key_hash not in load_clients():
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key_hash


@app.get("/v1/tokens/{token_address}/top-holders")
async def top_holders_endpoint(token_address: str, limit: int = 10, api_key: str = Depends(verify_api_key)):
    try:
        return await get_top_holders_report(token_address, logger, limit=limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid token address")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
