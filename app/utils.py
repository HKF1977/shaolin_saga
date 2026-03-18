import asyncio
import sys
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
import requests
import json
import os
import random
import aiohttp
from solders.pubkey import Pubkey
import re
import threading
import fcntl
import time
import base64
from io import BytesIO
from contextlib import contextmanager
from typing import Optional

# Import from your config
sys.path.append('/home/shaolin_saga/config')
from config import RPC_ENDPOINT, PUMP_PROGRAM, LAMPORTS_PER_SOL

# Custom JSON Encoder to handle special objects
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, BytesIO):
            return base64.b64encode(obj.getvalue()).decode('utf-8')
        return super().default(obj)

def is_valid_solana_address(address: str) -> bool:
    """Validate if a string is a valid Solana address"""
    try:
        Pubkey.from_string(address)
        return True
    except Exception:
        return False

def shorten_number(n):
    """Convert large numbers into short form (e.g., 1.2K, 3.4M)."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    elif n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.2f}K"
    else:
        return f"{n:.2f}"


def get_contract_uri_for_mint(mint: str) -> Optional[str]:
    """
    Returns the contract URI for a mint containing 'bonk' or 'pump'.
    
    """
    if "bonk" in mint:
        return f"https://bonk.fun/token/{mint}"
    elif "pump" in mint:
        return f"https://pump.fun/{mint}"
    else:
        return f"https://solscan.io/token/{mint}"


async def get_top_holders(client: AsyncClient, mint: Pubkey, limit: int = 10, max_retries: int = 3, logger=None):
    """Get top token holders for a given mint"""
    TOTAL_SUPPLY = 1000000000  # 1 billion tokens
    logger.info(f"Get top holders: {mint}")
    
    for attempt in range(max_retries):
        try:
            response = await client.get_token_largest_accounts(mint)

            # Check if response has value attribute and is not an error
            if not hasattr(response, 'value') or response.value is None:
                raise ValueError("Invalid response from RPC")

            if logger:
                logger.info(f"Number of accounts found: {len(response.value)}")
            
            holders = []
            for account in response.value:
                ui_amount = account.amount.ui_amount or 0.0
                if logger:
                    logger.info(f"Processing account: {account.address} with ui_amount: {ui_amount}")
                percentage = (ui_amount / TOTAL_SUPPLY) * 100
                short_address = f"{str(account.address)[:6]}...{str(account.address)[-4:]}"
                holders.append((short_address, percentage))

            # Sort by percentage and format display
            sorted_holders = sorted(holders, key=lambda x: x[1], reverse=True)
            holder_lines = []
            for idx, (addr, pct) in enumerate(sorted_holders[:limit], 1):
                holder_lines.append(f"{idx}. {addr}: {pct:.2f}%")
            
            return "\n".join(holder_lines)
            
        except Exception as e:
            if logger:
                logger.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                #logger.info(f"Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
                continue
            return "Holder data unavailable"


#Set tick or cross for socials
green_tick = "\u2705"
red_cross = "\u274C"

def format_social_link(url, platform):
    if url:
        return f"[{green_tick} {platform}]({url})"
    else:
        return f"{red_cross} {platform}"


async def get_saved_transaction_metadata(mint, logger=None):
    """Get token metadata from new file structure with safe file operations"""
    try:
        # Check metadata directory first
        metadata_file = f"/home/shaolin_saga/data/pump_data/metadata/{mint}.json"
        if safe_file_exists(metadata_file):
            logger.debug(f"Found metadata for {mint} in metadata directory")
            
            # SAFE JSON READ
            metadata = safe_json_read(metadata_file, default=None, logger=logger)
            if metadata is not None:
                return metadata  # Return as-is, already has correct field names
            else:
                logger.warning(f"Failed to read metadata from {metadata_file}")
        
        # If no saved metadata found, try fetching from pump.fun as fallback
        logger.debug(f"No saved metadata found for {mint}, trying pump.fun fallback")
        
        coin_data = await fetch_pump_fun_data(mint)
        if coin_data is not None:
            logger.info(f"Successfully fetched pump.fun data for {mint}")
            return {
                'name': coin_data.get('name', 'Unknown Token'),
                'symbol': coin_data.get('symbol', '???'),
                'image_url': coin_data.get('image_uri'),
                'description': coin_data.get('description'),
                'user': coin_data.get('creator'),
                'twitter_url': coin_data.get('twitter'),
                'telegram_url': coin_data.get('telegram'),
                'website_url': coin_data.get('website'),
                **coin_data  # Include all other fields as-is
            }
        else:
            logger.warning(f"Failed to fetch pump.fun data for {mint}")
        
        # If we get here, no metadata was found anywhere
        logger.debug(f"No metadata found for mint: {mint}")
        return None
        
    except Exception as e:
        logger.error(f"Error loading metadata for {mint}: {str(e)}")
        return None


async def get_saved_bonk_metadata(mint):
    """Get token metadata from new file structure"""
    try:
        # Check metadata directory
        metadata_file = f"/home/shaolin_saga/data/bonk_data/bonk_metadata/{mint}.json"
        if os.path.exists(metadata_file):
            with open(metadata_file, 'r') as f:
                return json.load(f)

    except Exception as e:
        websocket_logger.error(f"Error loading metadata for {mint}: {str(e)}")
        return None

async def get_token_metadata(client: AsyncClient, mint: Pubkey, logger=None):
    """Get token metadata from local storage or chain"""
    try:
        # First check if we have this token in our database
        token_file_path = f"/home/shaolin_saga/data/pump_data/metadata/{str(mint)}.json"
        try:
            with open(token_file_path, 'r') as f:
                token_data = json.load(f)
                return {
                    'name': token_data.get('name', 'Unknown Token'),
                    'symbol': token_data.get('symbol', '???'),
                    'image_url': token_data.get('image_url')
                }
        except FileNotFoundError:
            # Token not in our database, return default values
            if logger:
                logger.info(f"No local metadata found for token: {mint}")
            return {
                'name': 'Unknown Token',
                'symbol': '???',
                'image_url': None
            }
    except Exception as e:
        if logger:
            logger.error(f"Error getting token metadata: {str(e)}")
        return {
            'name': 'Unknown Token',
            'symbol': '???',
            'image_url': None
        }

async def get_token_metadata_by_mint(mint, logger=None):
    """
    Retrieve token metadata based on mint address.
    
    Args:
        mint: Token mint address (string)
        logger: Logger instance for debugging
        
    Returns:
        dict with keys: token_name, user, token_symbol, twitter_url, telegram_url, website_url
        Returns None if no metadata found
    """
    if "pump" in mint:
        tx_data = await get_saved_transaction_metadata(mint, logger)
        
        if tx_data:
            return {
                'token_name': tx_data.get('name', 'Unknown'),
                'user': tx_data.get('user', 'Unknown'),
                'token_symbol': tx_data.get('symbol', 'Unknown'),
                'twitter_url': tx_data.get('twitter_url'),
                'telegram_url': tx_data.get('telegram_url'),
                'website_url': tx_data.get('website_url')
            }
        else:
            logger.info(f"No pump metadata found for mint: {mint}")
            
    elif "bonk" in mint:
        tx_data = await get_saved_bonk_metadata(mint)
        
        if tx_data:
            return {
                'token_name': tx_data.get('name', 'Unknown'),
                'user': tx_data.get('user', 'Unknown'),
                'token_symbol': tx_data.get('symbol', 'Unknown'),
                'twitter_url': tx_data.get('twitter_url'),
                'telegram_url': tx_data.get('telegram_url'),
                'website_url': tx_data.get('website_url')
            }
        else:
            logger.info(f"No bonk metadata found for mint: {mint}")
    else:
        logger.info(f"No transaction data found: {mint}")
    
    return None

async def get_token_symbol(mint_address):
    """Get the symbol of a token"""
    try:
        tx_data = await get_saved_transaction_metadata(mint_address, logger=None)
        if tx_data and 'symbol' in tx_data:
            return tx_data['symbol']
        else:
            tx_data = await get_saved_bonk_metadata(mint_address)
            if tx_data and 'symbol' in tx_data:
                return tx_data['symbol']
            else:
                return None
    except Exception:
        return mint_address

async def get_moralis_usd_price(token_address: str, api_key: str, logger=None) -> dict:
    """
    Get USD price for a single token from Moralis API.
    
    Args:
        token_address: The Solana token mint address
        api_key: Your Moralis API key
        logger: Optional logger instance
        
    Returns:
        dict: Price data including usdPrice, symbol, name, etc. or None if failed
        
    Example response:
        {
            "tokenAddress": "So11111111111111111111111111111111111111112",
            "usdPrice": 78.39937984,
            "usdPrice24h": 77.392676261,
            "usdPrice24hrUsdChange": -0.7648736480000053,
            "usdPrice24hrPercentChange": -0.9883023626428632,
            "symbol": "SOL",
            "name": "Wrapped SOL",
            "logo": "https://logo.moralis.io/...",
            "exchangeName": "Orca Whirlpool",
            "nativePrice": {...}
        }
    """
    url = f"https://solana-gateway.moralis.io/token/mainnet/{token_address}/price"
    headers = {
        "accept": "application/json",
        "X-API-Key": api_key
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    if logger:
                        logger.error(f"Moralis API error for {token_address}: HTTP {response.status}")
                    return None
                
                data = await response.json()
                
                if logger:
                    logger.debug(f"Fetched price for {token_address}: ${data.get('usdPrice', 0)}")
                
                return data
                
    except Exception as e:
        if logger:
            logger.error(f"Error fetching Moralis price for {token_address}: {str(e)}")
        return None


# Simple in-memory price cache with 1-hour TTL
_price_cache = {}  # {token_address: {"data": {...}, "timestamp": float}}
PRICE_CACHE_TTL = 3600  # 1 hour in seconds

async def get_moralis_usd_price_cached(token_address: str, api_key: str, logger=None) -> dict:
    """
    Get USD price for a token with simple in-memory caching (1 hour TTL).
    Caches responses for 1 hour to reduce API calls.
    
    Args:
        token_address: The Solana token mint address
        api_key: Your Moralis API key
        logger: Optional logger instance
        
    Returns:
        dict: Price data or None if failed
    """
    current_time = time.time()
    
    # Check if we have cached data that's still valid
    if token_address in _price_cache:
        cache_entry = _price_cache[token_address]
        age = current_time - cache_entry["timestamp"]
        
        if age < PRICE_CACHE_TTL:
            if logger:
                logger.debug(f"Cache hit for {token_address} (age: {age:.0f}s)")
            return cache_entry["data"]
        else:
            if logger:
                logger.debug(f"Cache expired for {token_address} (age: {age:.0f}s)")
    
    # Cache miss or expired - fetch from API
    if logger:
        logger.debug(f"Fetching fresh price data for {token_address}")
    
    price_data = await get_moralis_usd_price(token_address, api_key, logger=logger)
    
    # Cache the result if successful
    if price_data is not None:
        _price_cache[token_address] = {
            "data": price_data,
            "timestamp": current_time
        }
        if logger:
            logger.debug(f"Cached price for {token_address}")
    
    return price_data


async def get_moralis_token_prices(mints: list, api_key: str, logger=None) -> dict:
    """
    Fetch token prices from Moralis batch endpoint (max 20 per call).
    Automatically chunks requests if more than 20 mints.
    """
    if not mints:
        return {}
    
    prices = {}
    batch_size = 20
    
    # Split into chunks of 20
    for i in range(0, len(mints), batch_size):
        batch = mints[i:i + batch_size]
        
        url = "https://solana-gateway.moralis.io/token/mainnet/prices"
        headers = {
            "accept": "application/json",
            "X-API-Key": api_key,
            "content-type": "application/json"
        }
        payload = {"addresses": batch}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        if logger:
                            logger.error(f"Moralis API error: {response.status}")
                        continue
                    
                    data = await response.json()
                    
                    for price_obj in data:
                        mint = price_obj.get("tokenAddress")
                        if mint:
                            prices[mint] = {
                                "usdPrice": price_obj.get("usdPrice", 0),
                                "symbol": price_obj.get("symbol", "UNKNOWN"),
                                "name": price_obj.get("name", "Unknown"),
                                "logo": price_obj.get("logo"),
                                "nativePrice": price_obj.get("nativePrice", {}).get("value"),
                                "exchangeName": price_obj.get("exchangeName")
                            }
        
        except Exception as e:
            if logger:
                logger.error(f"Error fetching Moralis batch: {str(e)}")
            continue
    
    if logger:
        logger.debug(f"Fetched prices for {len(prices)} tokens from Moralis ({(len(mints) + 19) // 20} batch calls)")
    
    return prices


async def get_token_price(mint_address, logger):
    """Get the current price of a token in USD (simplified)"""
    try:
        # Check if this is a pump.fun token
        filename = f"/home/shaolin_saga/data/pump_data/active_tokens/{mint_address}.json"
        
        if os.path.exists(filename):
            # This is a pump.fun token, get price from bonding curve
            with open(filename, 'r') as f:
                tx_data = json.load(f)
            
            if 'bondingCurve' in tx_data:
                bonding_curve = Pubkey.from_string(tx_data['bondingCurve'])
                
                # Get price from bonding curve
                async with AsyncClient(RPC_ENDPOINT) as client:
                    from pump_monk_new import get_bonding_curve_state, calculate_bonding_curve_price
                    curve_state = await get_bonding_curve_state(client, bonding_curve)
                    token_price_sol = calculate_bonding_curve_price(curve_state)
                    
                    # Get SOL price in USD
                    sol_price_usd = await get_sol_price_usd()
                    
                    # Calculate token price in USD
                    return token_price_sol * sol_price_usd
        
        # For other tokens, return None for now
        return None
    except Exception as e:
        logger.error(f"Error getting token price: {str(e)}")
        return None

async def get_sol_price_usd():
    """Get the current SOL price in USD"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['solana']['usd']
                return 100  # Fallback value if API fails
    except Exception:
        return 100  # Fallback value


async def get_metadata(uri):
    # Extract the IPFS hash from the URI
    ipfs_hash = uri[uri.index('/ipfs/') + 6:]

    # Replace the 4everland gateway with the ipfs.io gateway
    new_uri = 'https://pump.mypinata.cloud/ipfs/' + ipfs_hash
    # Make a request to the new URI
    #async with aiohttp.ClientSession() as session:
    #    async with session.get(new_uri) as response:
    #        return await response.json()


    response = requests.get(new_uri)
    return response.json()

async def get_image_data(image_uri):
    if image_uri and '/ipfs/' in image_uri:
        ipfs_hash = image_uri[image_uri.index('/ipfs/') + 6:]
        return 'https://pump.mypinata.cloud/ipfs/' + ipfs_hash
    return image_uri


async def fetch_pump_fun_data(mint_address, logger=None):
    """Fetch token data from pump.fun website for a given mint address.
    
    Args:
        mint_address (str): The mint address of the token
        logger: Logger instance to use (optional)
        
    Returns:
        dict: Token data including name, symbol, etc. or None if not found
    """
    url = f"https://pump.fun/{mint_address}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    if logger:
                        logger.warning(f"Failed to fetch pump.fun data for {mint_address}: Status {response.status}")
                    return None
                
                data = await response.text()
                
                # Unescape the data to handle any escaped characters
                unescaped_data = data.encode().decode('unicode_escape')
                
                # Use regex to extract the coin data JSON
                pattern = r'\"coin\":\s*(\{[^}]+\})'
                match = re.search(pattern, unescaped_data)
                
                if match:
                    coin_json = match.group(1)
                    try:
                        # Clean up the JSON string to make it valid
                        # Replace single quotes with double quotes for JSON validity
                        coin_json = coin_json.replace("'", '"')
                        
                        # Parse the JSON
                        coin_data = json.loads(coin_json)
                        
                        if logger:
                            logger.info(f"Successfully extracted pump.fun data for {mint_address}")
                        return coin_data
                        
                    except json.JSONDecodeError as e:
                        if logger:
                            logger.error(f"Found potential coin data for {mint_address}, but failed to parse JSON: {str(e)}")
                            logger.debug(f"Raw data: {coin_json[:200]}...")
                else:
                    if logger:
                        logger.warning(f"No coin data pattern found for {mint_address}")
                        
    except Exception as e:
        if logger:
            logger.error(f"Error fetching pump.fun data for {mint_address}: {str(e)}")
    
    return None


# File lock manager - shared across all modules
file_locks = {}
file_locks_lock = threading.Lock()

@contextmanager
def safe_file_operation(file_path, mode='r', timeout=10):
    """
    Context manager for safe file operations with locking
    """
    lock_key = os.path.abspath(file_path)
    
    # Get or create lock for this file
    with file_locks_lock:
        if lock_key not in file_locks:
            file_locks[lock_key] = threading.Lock()
        file_lock = file_locks[lock_key]
    
    # Acquire the lock with timeout
    acquired = file_lock.acquire(timeout=timeout)
    if not acquired:
        raise TimeoutError(f"Could not acquire lock for {file_path} within {timeout} seconds")
    
    try:
        # Retry logic for file operations
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if os.path.exists(file_path) or 'w' in mode or 'a' in mode:
                    with open(file_path, mode) as f:
                        yield f
                    break
                else:
                    raise FileNotFoundError(f"File not found: {file_path}")
            except (IOError, OSError) as e:
                if attempt == max_retries - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))  # Exponential backoff
    finally:
        file_lock.release()

def safe_json_read(file_path, default=None, logger=None):
    """Safely read JSON file with error handling"""
    try:
        with safe_file_operation(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        if logger:
            logger.debug(f"File not found: {file_path}")
        return default
    except json.JSONDecodeError as e:
        if logger:
            logger.error(f"JSON decode error in {file_path}: {e}")
        return default
    except TimeoutError as e:
        if logger:
            logger.warning(f"File lock timeout for {file_path}: {e}")
        return default
    except Exception as e:
        if logger:
            logger.error(f"Error reading {file_path}: {e}")
        return default

def safe_json_write(file_path, data, logger=None):
    """Safely write JSON file with atomic operations"""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # Write to temporary file first
        temp_path = f"{file_path}.tmp"
        
        with safe_file_operation(temp_path, 'w') as f:
            json.dump(data, f, indent=2)
        
        # Atomic rename
        os.rename(temp_path, file_path)
        
        if logger:
            logger.debug(f"Successfully wrote {file_path}")
        return True
        
    except Exception as e:
        if logger:
            logger.error(f"Error writing {file_path}: {e}")
        
        # Clean up temp file if it exists
        temp_path = f"{file_path}.tmp"
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        return False

def safe_file_move(src_path, dst_path, logger=None):
    """Safely move file between directories with proper locking"""
    try:
        # Ensure destination directory exists
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        
        # Use file locking for both source and destination
        src_lock_key = os.path.abspath(src_path)
        dst_lock_key = os.path.abspath(dst_path)
        
        with file_locks_lock:
            if src_lock_key not in file_locks:
                file_locks[src_lock_key] = threading.Lock()
            if dst_lock_key not in file_locks:
                file_locks[dst_lock_key] = threading.Lock()
        
        # Acquire both locks (in consistent order to prevent deadlock)
        locks_to_acquire = []
        if src_lock_key != dst_lock_key:  # Don't double-lock the same file
            locks_to_acquire = sorted([
                (file_locks[src_lock_key], src_lock_key),
                (file_locks[dst_lock_key], dst_lock_key)
            ], key=lambda x: x[1])  # Sort by path to prevent deadlock
        else:
            locks_to_acquire = [(file_locks[src_lock_key], src_lock_key)]
        
        # Acquire all locks
        acquired_locks = []
        try:
            for lock, path in locks_to_acquire:
                if lock.acquire(timeout=10):
                    acquired_locks.append(lock)
                else:
                    raise TimeoutError(f"Could not acquire lock for {path}")
            
            # Perform the move
            if os.path.exists(src_path):
                os.rename(src_path, dst_path)
                if logger:
                    logger.debug(f"Successfully moved {src_path} to {dst_path}")
                return True
            else:
                if logger:
                    logger.warning(f"Source file not found for move: {src_path}")
                return False
                
        finally:
            # Release all acquired locks
            for lock in acquired_locks:
                lock.release()
                
    except Exception as e:
        if logger:
            logger.error(f"Error moving {src_path} to {dst_path}: {e}")
        return False

def safe_file_exists(file_path):
    """Safely check if file exists"""
    try:
        return os.path.exists(file_path)
    except Exception:
        return False

def safe_file_delete(file_path, logger=None):
    """Safely delete a file with locking"""
    try:
        with safe_file_operation(file_path, 'r'):  # Just acquire the lock
            pass
        
        if os.path.exists(file_path):
            os.remove(file_path)
            if logger:
                logger.debug(f"Successfully deleted {file_path}")
            return True
        return False
        
    except Exception as e:
        if logger:
            logger.error(f"Error deleting {file_path}: {e}")
        return False


async def get_token_transactions(client, token_address, limit=100):
    """Get transaction signatures and data for a token"""
    try:
        # First, get transaction signatures for the token
        request_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                token_address,
                {"limit": limit}
            ]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(RPC_ENDPOINT, json=request_data) as response:
                if response.status != 200:
                    logger.error(f"Error fetching signatures: HTTP {response.status}")
                    return []

                result = await response.json()

                if "result" not in result:
                    logger.error(f"No result in signature response: {result}")
                    return []

                signatures = result["result"]

                # Now fetch transaction details for each signature
                transactions = []
                for sig_info in signatures:
                    signature = sig_info.get("signature")
                    if not signature:
                        continue

                    # Get transaction details
                    tx_request = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            signature,
                            {"encoding": "json", "maxSupportedTransactionVersion": 0}
                        ]
                    }

                    async with session.post(RPC_ENDPOINT, json=tx_request) as tx_response:
                        if tx_response.status != 200:
                            logger.warning(f"Failed to fetch transaction {signature}")
                            continue

                        tx_result = await tx_response.json()

                        if "result" not in tx_result or not tx_result["result"]:
                            continue

                        tx_data = tx_result["result"]

                        # Extract relevant information
                        transaction = {
                            "signature": signature,
                            "slot": tx_data.get("slot"),
                            "blockTime": tx_data.get("blockTime"),
                            "signer": tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])[0] if tx_data.get("transaction") else None,
                            "fee": tx_data.get("meta", {}).get("fee"),
                            "status": "success" if tx_data.get("meta", {}).get("err") is None else "failed"
                        }

                        # Try to extract SOL amount for buy transactions
                        if tx_data.get("meta") and tx_data.get("meta").get("preBalances") and tx_data.get("meta").get("postBalances"):
                            pre_balances = tx_data.get("meta").get("preBalances")
                            post_balances = tx_data.get("meta").get("postBalances")

                            if len(pre_balances) > 0 and len(post_balances) > 0:
                                # Calculate SOL difference for the signer
                                sol_diff = (pre_balances[0] - post_balances[0]) / 1_000_000_000  # Convert lamports to SOL
                                transaction["sol_amount"] = sol_diff if sol_diff > 0 else None

                        transactions.append(transaction)

                return transactions
    except Exception as e:
        logger.error(f"Error getting token transactions: {str(e)}")
        logger.error(traceback.format_exc())
        return []

async def analyze_for_bundling(client, transactions, token_address):
    """Analyze transactions for bundling patterns"""
    try:
        # Skip if no transactions
        if not transactions:
            return {
                "bundle_score": 0,
                "transaction_clusters": [],
                "wallet_analysis": {
                    "new_wallets": 0,
                    "total_wallets": 0,
                    "similar_buys": 0,
                    "supply_percentage": 0
                }
            }

        # Sort transactions by blockTime
        transactions.sort(key=lambda x: x.get("blockTime", 0))

        # Focus on the first 50 transactions or all if less than 50
        early_transactions = transactions[:min(50, len(transactions))]

        # 1. Find transaction clusters (transactions in the same or consecutive slots)
        clusters = []
        current_cluster = []

        for i, tx in enumerate(early_transactions):
            if not current_cluster:
                current_cluster = [tx]
                continue

            # Check if this transaction is close in time to the previous one
            prev_tx = current_cluster[-1]

            # If blockTime is available, use it for clustering
            if tx.get("blockTime") and prev_tx.get("blockTime"):
                time_diff = abs(tx["blockTime"] - prev_tx["blockTime"])

                # If transactions are within 0.5 seconds, consider them part of the same cluster
                if time_diff <= 0.5:
                    current_cluster.append(tx)
                else:
                    # If cluster has at least 3 transactions, add it to clusters
                    if len(current_cluster) >= 3:
                        clusters.append(current_cluster)

                    # Start a new cluster
                    current_cluster = [tx]

            # If blockTime is not available, use slot for clustering
            elif tx.get("slot") and prev_tx.get("slot"):
                slot_diff = abs(tx["slot"] - prev_tx["slot"])

                # If transactions are within 2 slots, consider them part of the same cluster
                if slot_diff <= 2:
                    current_cluster.append(tx)
                else:
                    # If cluster has at least 3 transactions, add it to clusters
                    if len(current_cluster) >= 3:
                        clusters.append(current_cluster)

                    # Start a new cluster
                    current_cluster = [tx]
            else:
                # If neither blockTime nor slot is available, just add to current cluster
                current_cluster.append(tx)

        # Add the last cluster if it has at least 3 transactions
        if len(current_cluster) >= 3:
            clusters.append(current_cluster)

        # 2. Analyze wallet relationships
        wallets = {}
        for tx in early_transactions:
            signer = tx.get("signer")
            if signer:
                if signer not in wallets:
                    wallets[signer] = {
                        "transactions": [],
                        "sol_amount": 0
                    }

                wallets[signer]["transactions"].append(tx)

                if tx.get("sol_amount"):
                    wallets[signer]["sol_amount"] += tx.get("sol_amount", 0)

        # 3. Look for similar transaction amounts
        sol_amounts = [tx.get("sol_amount") for tx in early_transactions if tx.get("sol_amount")]
        similar_buys = 0

        if sol_amounts:
            # Group amounts by similarity (within 10%)
            amount_groups = {}

            for amount in sol_amounts:
                found_group = False

                for group_key in amount_groups:
                    # If amount is within 10% of group key, add to that group
                    if abs(amount - group_key) / group_key <= 0.1:
                        amount_groups[group_key].append(amount)
                        found_group = True
                        break

                if not found_group:
                    amount_groups[amount] = [amount]

            # Count similar buys (groups with at least 3 transactions)
            for group in amount_groups.values():
                if len(group) >= 3:
                    similar_buys += len(group)

        # 4. Calculate bundle score
        bundle_score = 0

        # Factor 1: Percentage of transactions in clusters
        clustered_tx_count = sum(len(cluster) for cluster in clusters)
        cluster_percentage = clustered_tx_count / len(early_transactions) if early_transactions else 0
        bundle_score += cluster_percentage * 40  # Up to 40 points

        # Factor 2: Similar buy amounts
        similar_buys_percentage = similar_buys / len(early_transactions) if early_transactions else 0
        bundle_score += similar_buys_percentage * 30  # Up to 30 points

        # Factor 3: Largest cluster size relative to total transactions
        largest_cluster_size = max([len(cluster) for cluster in clusters]) if clusters else 0
        largest_cluster_percentage = largest_cluster_size / len(early_transactions) if early_transactions else 0
        bundle_score += largest_cluster_percentage * 30  # Up to 30 points

        # Cap score at 100
        bundle_score = min(100, bundle_score)

        # Format clusters for display
        formatted_clusters = []
        for cluster in clusters:
            if len(cluster) >= 3:  # Only include significant clusters
                start_time = min(tx.get("blockTime", 0) for tx in cluster if tx.get("blockTime"))
                end_time = max(tx.get("blockTime", 0) for tx in cluster if tx.get("blockTime"))
                timespan = end_time - start_time if start_time and end_time else 0

                formatted_clusters.append({
                    "count": len(cluster),
                    "timespan": timespan,
                    "slot": cluster[0].get("slot"),
                    "signatures": [tx.get("signature") for tx in cluster]
                })

        # Sort clusters by size (largest first)
        formatted_clusters.sort(key=lambda x: x["count"], reverse=True)

        # Calculate supply percentage (simplified)
        # In a real implementation, you would need to calculate the actual token supply controlled
        # For now, we'll use a simplified approach based on transaction count
        supply_percentage = min(100, (clustered_tx_count / len(early_transactions)) * 100) if early_transactions else 0

        return {
            "bundle_score": round(bundle_score),
            "transaction_clusters": formatted_clusters,
            "wallet_analysis": {
                "new_wallets": len(wallets),
                "total_wallets": len(wallets),
                "similar_buys": similar_buys,
                "supply_percentage": supply_percentage
            }
        }
    except Exception as e:
        logger.error(f"Error analyzing for bundling: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "bundle_score": 0,
            "transaction_clusters": [],
            "wallet_analysis": {
                "new_wallets": 0,
                "total_wallets": 0,
                "similar_buys": 0,
                "supply_percentage": 0
            }
        }

