from solders.pubkey import Pubkey

from utils import (
    is_valid_solana_address,
    get_token_metadata_by_mint,
    fetch_top_holders_raw,
    safe_json_read,
)


async def get_top_holders_report(token_address: str, logger, limit: int = 10) -> dict:
    """
    Assemble the full top-holders report for a token: metadata + structured holder data.
    Shared core used by both the /top-holders Discord command and the top-holders API endpoint.

    Returns:
    {
        "token_address": str,
        "token_name": str,
        "token_symbol": str,
        "image_url": str | None,
        "holders": [{"rank", "address", "ui_amount", "percentage", "is_bonding_curve"}, ...],
    }

    Raises ValueError if token_address is not a valid Solana address.
    """
    if not is_valid_solana_address(token_address):
        raise ValueError("Invalid token address")

    token_info = await get_token_metadata_by_mint(token_address, logger) or {}

    bonding_curve_account = None
    active_data = safe_json_read(
        f"/home/shaolin_saga/data/pump_data/active_tokens/{token_address}.json",
        default=None,
        logger=logger,
    )
    if active_data:
        bonding_curve_account = active_data.get('associatedBondingCurve')

    holders = await fetch_top_holders_raw(
        Pubkey.from_string(token_address),
        limit=limit,
        logger=logger,
        bonding_curve_account=bonding_curve_account,
    )

    return {
        "token_address": token_address,
        "token_name": token_info.get("token_name", "Unknown Token"),
        "token_symbol": token_info.get("token_symbol", "???"),
        "image_url": token_info.get("image_url"),
        "holders": holders,
    }
