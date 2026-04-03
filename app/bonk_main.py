import asyncio
import json
import base64
import struct
import time
import websockets
import logging
import datetime
import os
import sys
import requests
import discord
from logging.handlers import RotatingFileHandler
from discord.ext import commands
from dotenv import load_dotenv
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from utils import get_top_holders, safe_json_write
from rate_limiter import queue_discord_send, MessagePriority, get_message_queue, monitor_rate_limits, safe_rpc_call, log_rpc_stats, get_rpc_rate_limiter, ensure_queue_processing, monitor_memory_usage
from telegram_sender import queue_telegram_send, get_telegram_targets
from telegram_formatter import format_new_bonk_tokens

# Global variables
bot = None
servers = None

# Load environment variables
load_dotenv()

# Import config
sys.path.append('/home/shaolin_saga/config')
from config import WSS_ENDPOINT_SECONDARY, RPC_ENDPOINT_SECONDARY, SS_ICON_URL

# Bonk.fun constants
BONK_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
#BONK_CREATE_DISCRIMINATOR = "afaf6d1f0d989bed"
BONK_CREATE_DISCRIMINATORS = {
    "afaf6d1f0d989bed",
    "0b28a58f15000000",
    "4399af27da102620"
}



# Set up the logger
def setup_logger(name, log_file):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    os.makedirs('/home/shaolin_saga/logs', exist_ok=True)
    file_handler = RotatingFileHandler(f'/home/shaolin_saga/logs/{log_file}', maxBytes=1024*1024, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    return logger

# Create logger
bonk_logger = setup_logger('bonk', 'bonk_monitor.log')

# Load allowed servers
with open('/home/shaolin_saga/config/servers.json', 'r') as server_file:
    servers = json.load(server_file)


async def start_monitoring(bot_instance, servers_config):
    global bot, servers
    bot = bot_instance
    servers = servers_config

    bonk_logger.info("Starting Bonk monitoring")
    bonk_logger.info(f"Using bot: {bot.user}")

    # Make sure the required directories exist
    os.makedirs("/home/shaolin_saga/data/bonk_data/active_bonk_tokens", exist_ok=True)

    # Start the monitoring tasks (from the original on_ready)
    bot.loop.create_task(listen_for_bonk_tokens())

    bonk_logger.info("Bonk monitoring started successfully")

# Utility functions (move to utils)
#def get_channel(server_id, channel_type):
#    """Get channel for a specific server"""
#    for server in servers['allowed_servers']:
#        if server['server_id'] == server_id:
#            channel = bot.get_channel(server['channels'][channel_type])
#            return channel
#    return None

def get_channel(server_id, channel_type):
    for server in servers['allowed_servers']:
        if server['server_id'] == server_id:
            channel_id = server['channels'].get(channel_type)  # Returns None if missing
            if channel_id:
                return bot.get_channel(channel_id)
    return None

def save_active_bonk_token(data):
    """Save minimal data for bonding curve monitoring with safe file operations"""
    filename = f"/home/shaolin_saga/data/bonk_data/active_bonk_tokens/{data['mint']}.json"

    active_data = {
        'mint': data['mint'],
        'bondingCurve': data['bonding_curve'],
        'raydium_market': data['raydium_market'],
        'raydium_pool_1': data['raydium_pool_1'],
        'pool_vaults': data.get('pool_vaults', []),
        'user': data['creator'],
        'created': time.time()
    }

    # SAFE JSON WRITE - handles directory creation and atomic writes
    if safe_json_write(filename, active_data, logger=bonk_logger):
        bonk_logger.debug(f"Successfully saved active bonk token data for {data['mint']}")
        return True
    else:
        bonk_logger.error(f"Failed to save active bonk token data for {data['mint']}")
        return False


def save_bonk_token_metadata(mint, enriched_data):
    """Save processed metadata for embeds with safe file operations"""
    filename = f"/home/shaolin_saga/data/bonk_data/bonk_metadata/{mint}.json"
    
    # SAFE JSON WRITE - handles directory creation and atomic writes
    if safe_json_write(filename, enriched_data, logger=bonk_logger):
        bonk_logger.debug(f"Successfully saved bonk metadata for {mint}")
        return True
    else:
        bonk_logger.error(f"Failed to save bonk metadata for {mint}")
        return False


async def get_metadata(uri):
    """Extract metadata from IPFS URI - simple and minimal for Bonk tokens"""
    try:
        response = requests.get(uri, timeout=10)
        
        if response.status_code == 200:
            return response.json()
        else:
            bonk_logger.warning(f"Failed to fetch metadata: HTTP {response.status_code}")
            return {}
            
    except Exception as e:
        bonk_logger.error(f"Error fetching metadata: {str(e)}")
        return {}


# Social media formatting
green_tick = "\u2705"
red_cross = "\u274C"

def format_social_link(url, platform):
    if url:
        return f"[{green_tick} {platform}]({url})"
    else:
        return f"{red_cross} {platform}"


# Function to check if DexScreener data exists for a mint
def has_dexscreener_data(mint):
    """Check if DexScreener data exists for the given mint"""
    filename = f"/home/shaolin_saga/data/dex_data/dexscreener/{mint}.json"
    return os.path.exists(filename)


async def create_bonk_embed(token_name, token_symbol, mint, image_url, description, creator, twitter_url, telegram_url, website_url):
    #async with AsyncClient(RPC_ENDPOINT_SECONDARY) as client:
    #    await asyncio.sleep(9)  # Wait for RPC indexing
    #    top_holders = await get_top_holders(client, Pubkey.from_string(mint), logger=bonk_logger)
    #    print(f'{top_holders} from get_top_holders')

        contract_uri = f'https://bonk.fun/token/{mint}'
        creator_uri = 'https://solscan.io/account/' + creator
        embed = discord.Embed(
            title=f"{token_name} ({token_symbol})",
            color=0xFFD700,
            timestamp=datetime.datetime.utcnow()
        )
        
        embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

        embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
        embed.add_field(name="", value=f'```{mint}```', inline=False)
        embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
        embed.add_field(name="", value=f'```{creator}```', inline=False)

        # Truncate desription
        if len(description) > 1000:
            description = description[:997] + "..."
        embed.add_field(name="Description", value=f'```{description}```', inline=False)

        if image_url:
            embed.set_thumbnail(url=image_url)
    
        # Add social media links
        embed.add_field(name="Social Media", value="", inline=False)
        embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
        embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
        embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

        # Add DexScreener indicator
        dex_status = green_tick if has_dexscreener_data(mint) else red_cross
        embed.add_field(name="Dex Paid?", value=dex_status, inline=True)

        # Add top holders
        #if top_holders:
        #    embed.add_field(name="Top Holders", value=f'```{top_holders}```', inline=False)

        current_unix_time = int(time.time())
        embed.add_field(name="Created Time", value=f"<t:{current_unix_time}:R>", inline=False)
    
        # Hotkeys section
        hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ BONK ](https://bonk.fun/token/{mint})"
        )
        embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
        embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
    
        return embed

def decode_bonk_create_instruction(ix_data):
    """Decode Bonk token creation instruction data."""
    try:
        data = bytes(ix_data)
        offset = 9  # Skip 8-byte discriminator + 1 additional byte
        
        # Read token name
        name_length = struct.unpack('<I', data[offset:offset+4])[0]
        offset += 4
        token_name = data[offset:offset+name_length].decode('utf-8')
        offset += name_length
        
        # Read token symbol
        symbol_length = struct.unpack('<I', data[offset:offset+4])[0]
        offset += 4
        token_symbol = data[offset:offset+symbol_length].decode('utf-8')
        offset += symbol_length
        
        # Read metadata URI
        uri_length = struct.unpack('<I', data[offset:offset+4])[0]
        offset += 4
        metadata_uri = data[offset:offset+uri_length].decode('utf-8')
        
        bonk_logger.info(f"✅ Decoded - Name: '{token_name}', Symbol: '{token_symbol}', URI: '{metadata_uri}'")
        
        return {
            'name': token_name.strip(),
            'symbol': token_symbol.strip(),
            'uri': metadata_uri.strip()
        }
        
    except Exception as e:
        bonk_logger.error(f"Error decoding instruction: {str(e)}")
        #bonk_logger.error(f"Raw data (first 200 bytes): {ix_data.hex()}")
        return None


def extract_bonk_accounts(accounts):
    """Extract key accounts from Bonk token creation transaction"""
    try:
        # Find the real mint (ends with 'bonk')
        mint_address = None
        for account in accounts:
            if account.lower().endswith('bonk'):
                mint_address = account
                break
        
        if not mint_address:
            bonk_logger.error("No mint address ending with 'bonk' found")
            return {}
        
        # Based on your analysis:
        account_mapping = {
            'creator': accounts[0] if len(accounts) > 0 else None,  # Developer/creator
            'mint': mint_address,  # The one ending with 'bonk'
            'bonding_curve': None,  # We need to find this
            'raydium_launchpad_auth': accounts[4] if len(accounts) > 4 else None,  # WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh
            'raydium_market': None
        }
        
        # Find bonding curve - look for the account that's not a known system account
        known_accounts = {
            'So11111111111111111111111111111111111111112',  # SOL
            'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',  # Token Program
            'metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s',  # Metaplex
            '11111111111111111111111111111111',  # System Program
            'SysvarRent111111111111111111111111111111111',  # Rent Sysvar
            '2DPAtwB8L12vrMRExbLuyGnC7n2J5LNoZQSejeQGpwkr',  # Known program
            'LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj',  # Bonk Program
            '6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX',  # Raydium (ants coin)
            'FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1',  # Bonk platform config
            'WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh',  # Raydium auth
            'BuM6KDpWiTcxvrpXywWFiw45R2RNH8WURdvqoTDV1BW4', # Bonkfun Platfrom Config
            account_mapping['creator'],  # Creator
            mint_address,  # Mint
        }

        # Skip first 2 accounts (creator duplicates), then filter out known accounts
        filtered_accounts = []
        for i, account in enumerate(accounts[2:], start=2):  # Start from index 2
            if account not in known_accounts:
                filtered_accounts.append(account)
        
        bonk_logger.info(f"🔍 Filtered accounts: {filtered_accounts}")
        
        account_mapping['raydium_market'] = filtered_accounts[0] if len(filtered_accounts) > 0 else None  # Raydium Market 
        account_mapping['raydium_pool_1'] = filtered_accounts[1] if len(filtered_accounts) > 1 else None  # Raydium pool 1 
        account_mapping['bonding_curve'] = filtered_accounts[1] if len(filtered_accounts) > 1 else None  # Bonk bonding curve 
        bonk_logger.info(f"🔍 Final accounts: {account_mapping}")
        
        return account_mapping
        

    except Exception as e:
        bonk_logger.error(f"Error extracting accounts: {str(e)}")
        return {}


async def handle_bonk_token_creation(decoded_data, accounts):
    """Handle new Bonk token creation"""
    try:
        # Extract key accounts
        account_info = extract_bonk_accounts(accounts)
        
        if not account_info.get('mint'):
            bonk_logger.error("Could not extract mint address")
            return
        
        mint_address = account_info['mint']
        creator = account_info['creator']
        bonding_curve = account_info['bonding_curve']
        raydium_market_address = account_info['raydium_market']
        raydium_pool_1 = account_info['raydium_pool_1']

        bonk_logger.info(f"🚀 NEW BONK TOKEN DETECTED!")
        bonk_logger.info(f"Name: {decoded_data['name']}")
        bonk_logger.info(f"Symbol: {decoded_data['symbol']}")
        bonk_logger.info(f"🪙 Mint: {mint_address}")
        bonk_logger.info(f"👤 Creator: {creator}")
        bonk_logger.info(f"🔄 Bonding Curve: {bonding_curve}")
        
        # Fetch metadata
        metadata = await get_metadata(decoded_data['uri'])
        
        # Create complete token data
        token_data = {
            'mint': mint_address,
            'creator': creator,
            'bonding_curve': bonding_curve,
            'raydium_market' : raydium_market_address,
            'raydium_pool_1' : raydium_pool_1,
            'name': decoded_data['name'],
            'symbol': decoded_data['symbol'],
            'uri': decoded_data['uri'],
            'metadata': metadata,
            'image_url': metadata.get('image'),
            'description': metadata.get('description') or 'No Description Added',
            'twitter_url': metadata.get('twitter'),
            'telegram_url': metadata.get('telegram'),
            'website_url': metadata.get('website'),
            'accounts': accounts,
            'account_mapping': account_info,  # Include the mapping for debugging
            'timestamp': datetime.datetime.now().isoformat(),
            'bonk_url': f'https://bonk.fun/{mint_address}'
        }
      
        # Save to both directories
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save minimal data for processing bonding curve
        save_active_bonk_token(token_data)


        # Save metadata to different location
        save_bonk_token_metadata(mint_address, token_data)


        # Create and send Discord embed
        embed = await create_bonk_embed(
            token_data['name'],
            token_data['symbol'],
            mint_address,
            token_data.get('image_url'),
            token_data.get('description'),
            token_data.get('creator'),
            token_data.get('twitter_url'),
            token_data.get('telegram_url'),
            token_data.get('website_url'),
        )
        
        # Send to all configured servers
        for server in servers['allowed_servers']:
            server_id = server['server_id']
            
            # Try multiple channel options for Bonk tokens
            channel = None
            
            # First try dedicated bonk channel
            if 'new_bonk_tokens' in server['channels']:
                channel = get_channel(server_id, 'new_bonk_tokens')
            
            if channel:
                try:
                    #await channel.send(embed=embed)
                    await queue_discord_send(channel, embed, "new_bonk_tokens", bonk_logger, MessagePriority.HIGH)
                    bonk_logger.info(f"✅ Sent Bonk token embed to server {server_id} channel {channel.name}")
                except Exception as e:
                    bonk_logger.error(f"❌ Failed to send embed to server {server_id}: {str(e)}")
            else:
                bonk_logger.warning(f"⚠️ No suitable channel found for server {server_id}")

        # Telegram: new_bonk_tokens
        tg_text = format_new_bonk_tokens(token_data)
        for target in get_telegram_targets('new_bonk_tokens'):
            await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, 'new_bonk_tokens', bonk_logger, delay_seconds=target.get('delay_seconds', 0))
        
        # Console output
        print("=" * 50)
        print("🎉 NEW BONK TOKEN LAUNCHED!")
        print(f"📛 Name: {decoded_data['name']}")
        print(f"🏷️  Symbol: {decoded_data['symbol']}")
        print(f"🪙 Mint: {mint_address}")
        print(f"🌐 Bonk URL: https://bonk.fun/{mint_address}")
        if token_data.get('image_url'):
            print(f"🖼️  Image: {token_data.get('image_url')}")
        if  token_data.get('twitter_url'):
            print(f"🐦 Twitter: { token_data.get('twitter_url')}")
        if token_data.get('telegram_url'):
            print(f"🐦 Telegram: {token_data.get('telegram_url')}")
        if token_data.get('website_url'):
            print(f"🐦 Website: {token_data.get('website_url')}")
        print("=" * 50)
        
    except Exception as e:
        bonk_logger.error(f"Error handling Bonk token creation: {str(e)}")
        import traceback
        bonk_logger.error(traceback.format_exc())


async def process_transaction(tx_data_decoded, signature=None):
    """Process transaction and look for Bonk token creation"""
    try:
        transaction = VersionedTransaction.from_bytes(tx_data_decoded)
        #bonk_logger.debug(f"Transaction from process transaction {transaction}")

        for ix_idx, ix in enumerate(transaction.message.instructions):
            program_idx = ix.program_id_index
            program_id = str(transaction.message.account_keys[program_idx])

            if program_id == BONK_PROGRAM:
                ix_data = bytes(ix.data)
                if len(ix_data) >= 8:
                    discriminator_hex = ix_data[:8].hex()
                    accounts_preview = [str(transaction.message.account_keys[idx]) for idx in ix.accounts if idx < len(transaction.message.account_keys)]
                    bonk_logger.info(f"🔍 BONK ix [{ix_idx}] discriminator={discriminator_hex} accounts={accounts_preview}")

                    if discriminator_hex in BONK_CREATE_DISCRIMINATORS:
                        bonk_logger.info(f"🎯 Token creation discriminator found: {discriminator_hex}")
                        # Decode the instruction
                        decoded_data = decode_bonk_create_instruction(ix_data)
                        ##bonk_logger.debug(f"Decoded data from process transaction {decoded_data}")
                        if decoded_data:
                            # Get accounts with bounds checking
                            accounts = []
                            total_account_keys = len(transaction.message.account_keys)
                            
                            for idx in ix.accounts:
                                if idx < total_account_keys:
                                    accounts.append(str(transaction.message.account_keys[idx]))
                                else:
                                    bonk_logger.warning(f"Account index {idx} out of range (total: {total_account_keys})")

                            # Handle the token creation
                            await handle_bonk_token_creation(decoded_data, accounts)
                        
    except Exception as e:
        bonk_logger.error(f"Error processing transaction: {str(e)}")
        import traceback
        bonk_logger.error(traceback.format_exc())


async def listen_for_bonk_tokens():
    """Listen for Bonk token creation with improved websocket handling"""
    #rpc_monitor = RPCMonitor()  # Uncomment if you have RPCMonitor initialized
    
    while True:  # Outer reconnection loop
        try:
            async with websockets.connect(
                WSS_ENDPOINT_SECONDARY,
                ping_interval=15,
                ping_timeout=10,
                close_timeout=5
            ) as websocket:
                bonk_logger.info("WebSocket connected successfully")

                subscription_message = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "blockSubscribe",
                    "params": [
                        {"mentionsAccountOrProgram": BONK_PROGRAM},
                        {
                            "commitment": "confirmed",
                            "encoding": "base64",
                            "showRewards": False,
                            "transactionDetails": "full",
                            "maxSupportedTransactionVersion": 0
                        }
                    ]
                })
                
                # Add reconnection delay
                await asyncio.sleep(2)

                await websocket.send(subscription_message)
                bonk_logger.info(f"🔍 Subscribed to Bonk program: {BONK_PROGRAM}")

                while True:  # Inner message processing loop
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=30)
                        #rpc_monitor.add_request()
                        #current_rate = rpc_monitor.get_current_rate()
                        
                        # Add validation before parsing
                        if not response or not response.strip():
                            bonk_logger.warning("Received empty response, skipping...")
                            continue

                        data = json.loads(response)
                        
                        if 'error' in data:
                            error_code = data['error'].get('code')
                            error_message = data['error'].get('message', '')
                            
                            if error_code == -32005 or 'RPS limit' in error_message:
                                # Extract wait time from the error
                                try_again_in = data['error'].get('data', {}).get('try_again_in', '100ms')
                                bonk_logger.error(f"🚫 Rate limited! Waiting {try_again_in} before retry")
                                
                                # Parse the wait time properly
                                wait_match = re.search(r'(\d+(?:\.\d+)?)', try_again_in)
                                if wait_match:
                                    wait_time = float(wait_match.group(1))
                                    
                                    # Convert to seconds based on unit
                                    if 'ms' in try_again_in.lower():
                                        wait_seconds = wait_time / 1000  # Convert ms to seconds
                                    elif 's' in try_again_in.lower() and 'ms' not in try_again_in.lower():
                                        wait_seconds = wait_time  # Already in seconds
                                    else:
                                        wait_seconds = wait_time / 1000  # Default to ms
                                    
                                    bonk_logger.info(f"⏳ Sleeping for {wait_seconds:.3f} seconds")
                                    await asyncio.sleep(wait_seconds + 0.1)  # Add 100ms buffer
                                else:
                                    # Fallback if parsing fails
                                    bonk_logger.warning("Could not parse wait time, using default 200ms")
                                    await asyncio.sleep(0.2)
                                
                                # Force reconnection to retry subscription
                                bonk_logger.info("🔄 Retrying subscription after rate limit")
                                break
                            else:
                                bonk_logger.error(f"RPC Error: {error_message}")
                                continue

                        # Message type handling
                        message_type = data.get('method', 'Unknown')
                        
                        if message_type == 'blockNotification':
                            if 'params' in data and 'result' in data['params']:
                                block_data = data['params']['result']
                                if 'value' in block_data and 'block' in block_data['value']:
                                    block = block_data['value']['block']
                                    if 'transactions' in block:
                                        for tx in block['transactions']:
                                            if isinstance(tx, dict) and 'transaction' in tx:
                                                tx_data_decoded = base64.b64decode(tx['transaction'][0])
                                                signature = tx.get('signatures', ['unknown'])[0] if 'signatures' in tx else 'unknown'
                                                await process_transaction(tx_data_decoded, signature)
                        
                        elif 'result' in data:
                            bonk_logger.info("✅ Bonk subscription confirmed")
                        else:
                            bonk_logger.warning(f"❓ Unhandled message type: {message_type}")
                            bonk_logger.debug(f"Full message: {data}")
                    
                    except asyncio.TimeoutError:
                        if not websocket.open:
                            bonk_logger.info("Connection lost during timeout, triggering reconnect")
                            break  # Break inner loop to reconnect

                        try:
                            pong = await websocket.ping()
                            await asyncio.wait_for(pong, timeout=5)
                            bonk_logger.debug("Ping successful")
                        except Exception as ping_error:
                            bonk_logger.warning(f"Ping failed: {ping_error}, triggering reconnect")
                            break  # Break inner loop to reconnect

                    except json.JSONDecodeError as json_error:
                        bonk_logger.error(f"JSON decode error: {json_error}")
                        continue  # Skip this message, continue processing

                    except Exception as msg_error:
                        bonk_logger.error(f"Message processing error: {msg_error}")

                        # Check for any connection-related errors that require reconnection
                        error_str = str(msg_error).lower()
                        connection_errors = [
                            "keepalive ping timeout",
                            "no close frame received",
                            "no close frame sent",
                            "connection closed",
                            "1011",
                            "1001",
                            "1006",
                            "connection lost"
                        ]

                        if any(error in error_str for error in connection_errors):
                            bonk_logger.error("WebSocket connection unstable, forcing reconnection")
                            break  # Exit inner loop to trigger reconnection
                        continue  # Skip this message, continue processing

        except websockets.exceptions.ConnectionClosedError as conn_error:
            bonk_logger.error(f"WebSocket connection closed: {conn_error}")
            bonk_logger.info("Attempting reconnection in 5 seconds...")
            await asyncio.sleep(5)
        
        except Exception as e:
            bonk_logger.error(f"Unexpected connection error: {e}")
            bonk_logger.info("Attempting reconnection in 5 seconds...")
