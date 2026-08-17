import asyncio
import json
import base64
import base58
import struct
import time
import websockets
import logging
import datetime
import os
import sys
import re
import requests
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import discord
from solders.transaction import VersionedTransaction
from utils import safe_json_write
from rate_limiter import queue_discord_send, MessagePriority
from telegram_sender import queue_telegram_send, get_telegram_targets
from telegram_formatter import format_new_nft_collection

# Global variables
bot = None
servers = None

# Load environment variables
load_dotenv()

# Import config
sys.path.append('/home/shaolin_saga/config')
from config import WSS_MOMENTUM_ENDPOINT, SS_ICON_URL

# mpl-core (Metaplex Core) program
MPL_CORE_PROGRAM = "CoREENxT6tW1HoK8ypY1SxRMZTcVPm7R94rH4PZNhX7d"

# mpl-core is Shank-built, not Anchor -- instructions use a 1-byte discriminant,
# not Anchor's 8-byte sighash. CreateCollectionV1 = 1, CreateCollectionV2 = 21.
CREATE_COLLECTION_DISCRIMINATORS = {1, 21}


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
nft_logger = setup_logger('nft', 'nft_monitor.log')

# Load allowed servers
with open('/home/shaolin_saga/config/servers.json', 'r') as server_file:
    servers = json.load(server_file)


async def start_monitoring(bot_instance, servers_config):
    global bot, servers
    bot = bot_instance
    servers = servers_config

    nft_logger.info("Starting NFT collection monitoring")
    nft_logger.info(f"Using bot: {bot.user}")

    # Make sure the required directories exist
    os.makedirs("/home/shaolin_saga/data/nft_data/collections", exist_ok=True)

    # Start the monitoring task
    bot.loop.create_task(listen_for_new_collections())

    nft_logger.info("NFT collection monitoring started successfully")


def get_channel(server_id, channel_type):
    for server in servers['allowed_servers']:
        if server['server_id'] == server_id:
            channel_id = server['channels'].get(channel_type)  # Returns None if missing
            if channel_id:
                return bot.get_channel(channel_id)
    return None


def save_collection_record(collection_address, data):
    """Save raw collection record to disk -- also the data source once we build velocity tracking"""
    filename = f"/home/shaolin_saga/data/nft_data/collections/{collection_address}.json"

    if safe_json_write(filename, data, logger=nft_logger):
        nft_logger.debug(f"Successfully saved collection record for {collection_address}")
        return True
    else:
        nft_logger.error(f"Failed to save collection record for {collection_address}")
        return False


async def get_metadata(uri):
    """Fetch off-chain metadata JSON from a collection's uri"""
    if not uri:
        return {}
    try:
        response = requests.get(uri, timeout=10)

        if response.status_code == 200:
            return response.json()
        else:
            nft_logger.warning(f"Failed to fetch metadata: HTTP {response.status_code}")
            return {}

    except Exception as e:
        nft_logger.error(f"Error fetching metadata: {str(e)}")
        return {}


# Social media formatting
green_tick = "✅"
red_cross = "❌"

def format_social_link(url, platform):
    if url:
        return f"[{green_tick} {platform}]({url})"
    else:
        return f"{red_cross} {platform}"


def decode_create_collection_instruction(ix_data):
    """Decode CreateCollectionV1/V2 instruction data.

    mpl-core args are Borsh-encoded: 1-byte discriminant, then name/uri as
    4-byte LE length-prefixed UTF-8 strings. Plugins (and, for V2, external
    plugin adapters) follow but aren't needed for collection detection.
    """
    try:
        data = bytes(ix_data)
        discriminator = data[0]
        offset = 1

        name_length = struct.unpack('<I', data[offset:offset+4])[0]
        offset += 4
        name = data[offset:offset+name_length].decode('utf-8')
        offset += name_length

        uri_length = struct.unpack('<I', data[offset:offset+4])[0]
        offset += 4
        uri = data[offset:offset+uri_length].decode('utf-8')

        nft_logger.info(f"✅ Decoded collection - Name: '{name}', URI: '{uri}' (discriminator={discriminator})")

        return {
            'name': name.strip(),
            'uri': uri.strip(),
        }

    except Exception as e:
        nft_logger.error(f"Error decoding collection instruction: {str(e)}")
        return None


async def create_collection_embed(name, collection_address, creator, image_url, description, twitter_url, telegram_url, website_url):
    embed = discord.Embed(
        title=f"{name}",
        color=0x9B59B6,
        timestamp=datetime.datetime.utcnow()
    )

    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

    collection_uri = f'https://solscan.io/account/{collection_address}'
    creator_uri = f'https://solscan.io/account/{creator}'

    embed.add_field(name="", value=f"[ Collection Address ]({collection_uri})", inline=False)
    embed.add_field(name="", value=f'```{collection_address}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{creator}```', inline=False)

    if description:
        if len(description) > 1000:
            description = description[:997] + "..."
        embed.add_field(name="Description", value=f'```{description}```', inline=False)

    if image_url:
        embed.set_thumbnail(url=image_url)

    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

    current_unix_time = int(time.time())
    embed.add_field(name="Created Time", value=f"<t:{current_unix_time}:R>", inline=False)

    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

    return embed


async def handle_new_collection(decoded_data, collection_address, creator):
    """Handle newly detected mpl-core collection creation"""
    try:
        nft_logger.info(f"🖼️ NEW NFT COLLECTION DETECTED!")
        nft_logger.info(f"Name: {decoded_data['name']}")
        nft_logger.info(f"🖼️ Collection: {collection_address}")
        nft_logger.info(f"👤 Creator: {creator}")

        # Fetch metadata
        metadata = await get_metadata(decoded_data['uri'])

        # Create complete collection data
        collection_data = {
            'collection': collection_address,
            'creator': creator,
            'name': decoded_data['name'],
            'uri': decoded_data['uri'],
            'metadata': metadata,
            'image_url': metadata.get('image'),
            'description': metadata.get('description') or 'No Description Added',
            'twitter_url': metadata.get('twitter'),
            'telegram_url': metadata.get('telegram'),
            'website_url': metadata.get('website'),
            'timestamp': datetime.datetime.now().isoformat(),
            'created': time.time(),
        }

        # Save record
        save_collection_record(collection_address, collection_data)

        # Create and send Discord embed
        embed = await create_collection_embed(
            collection_data['name'],
            collection_address,
            creator,
            collection_data.get('image_url'),
            collection_data.get('description'),
            collection_data.get('twitter_url'),
            collection_data.get('telegram_url'),
            collection_data.get('website_url'),
        )

        # Send to all configured servers
        for server in servers['allowed_servers']:
            server_id = server['server_id']

            channel = None
            if 'new_nft_collections' in server['channels']:
                channel = get_channel(server_id, 'new_nft_collections')

            if channel:
                try:
                    await queue_discord_send(channel, embed, "new_nft_collections", nft_logger, MessagePriority.MEDIUM)
                    nft_logger.info(f"✅ Sent NFT collection embed to server {server_id} channel {channel.name}")
                except Exception as e:
                    nft_logger.error(f"❌ Failed to send embed to server {server_id}: {str(e)}")
            else:
                nft_logger.warning(f"⚠️ No suitable channel found for server {server_id}")

        # Telegram: new_nft_collections
        tg_text = format_new_nft_collection(collection_data)
        for target in get_telegram_targets('new_nft_collections'):
            await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, 'new_nft_collections', nft_logger, delay_seconds=target.get('delay_seconds', 0))

        # Console output
        print("=" * 50)
        print("🖼️ NEW NFT COLLECTION!")
        print(f"📛 Name: {decoded_data['name']}")
        print(f"🖼️ Collection: {collection_address}")
        print(f"👤 Creator: {creator}")
        if collection_data.get('image_url'):
            print(f"🖼️  Image: {collection_data.get('image_url')}")
        print("=" * 50)

    except Exception as e:
        nft_logger.error(f"Error handling new collection: {str(e)}")
        import traceback
        nft_logger.error(traceback.format_exc())


async def process_transaction(tx_data_decoded, meta=None):
    """Process transaction and look for mpl-core collection creation, both as a
    top-level instruction and invoked via CPI from another program (e.g. a
    launchpad/marketplace program). CPI calls only show up in `meta.innerInstructions`,
    not in the signed transaction itself, so `meta` has to come from the block
    notification's tx entry alongside the raw transaction bytes.
    """
    try:
        transaction = VersionedTransaction.from_bytes(tx_data_decoded)
        tx_signature = str(transaction.signatures[0]) if transaction.signatures else 'unknown'

        static_keys = [str(k) for k in transaction.message.account_keys]

        # Versioned transactions can reference addresses loaded via address lookup
        # tables -- those are resolved by the RPC and returned in meta.loadedAddresses.
        # Any instruction (top-level or inner) can index into them beyond static_keys.
        loaded_writable, loaded_readonly = [], []
        if meta:
            loaded = meta.get('loadedAddresses') or {}
            loaded_writable = loaded.get('writable') or []
            loaded_readonly = loaded.get('readonly') or []
        full_account_keys = static_keys + loaded_writable + loaded_readonly

        mpl_core_mentioned = MPL_CORE_PROGRAM in full_account_keys
        mpl_core_top_level = False

        def resolve_accounts(idx_list):
            resolved = []
            for idx in idx_list:
                if idx < len(full_account_keys):
                    resolved.append(full_account_keys[idx])
                else:
                    nft_logger.warning(f"Account index {idx} out of range (total: {len(full_account_keys)})")
            return resolved

        async def maybe_handle_create(ix_data, account_idx_list, source):
            if len(ix_data) < 1:
                return
            discriminator = ix_data[0]
            nft_logger.debug(f"mpl-core {source} ix seen: discriminator={discriminator} sig={tx_signature}")

            if discriminator not in CREATE_COLLECTION_DISCRIMINATORS:
                return

            nft_logger.info(f"🎯 Collection creation discriminator found ({source}): {discriminator} sig={tx_signature}")
            decoded_data = decode_create_collection_instruction(ix_data)
            if not decoded_data:
                return

            accounts = resolve_accounts(account_idx_list)
            if not accounts:
                nft_logger.error(f"No accounts found for {source} collection creation instruction")
                return

            # accounts[0] is always the collection account -- it's required and
            # listed first, so it's stable regardless of whether the optional
            # updateAuthority account was included.
            collection_address = accounts[0]

            # Fee payer is always account_keys[0] on the transaction, so use it as
            # the creator rather than trusting a fixed ix.accounts index
            # (updateAuthority being optional can shift later indices).
            creator = static_keys[0]

            await handle_new_collection(decoded_data, collection_address, creator)

        # Top-level instructions
        for ix in transaction.message.instructions:
            program_id = full_account_keys[ix.program_id_index] if ix.program_id_index < len(full_account_keys) else None
            if program_id == MPL_CORE_PROGRAM:
                mpl_core_top_level = True
                await maybe_handle_create(bytes(ix.data), ix.accounts, "top-level")

        # Inner (CPI) instructions -- only present in meta, not in the raw tx bytes.
        # Instruction data here comes back base58-encoded regardless of the
        # block subscription's outer `encoding` setting.
        if meta and meta.get('innerInstructions'):
            for entry in meta['innerInstructions']:
                for inner_ix in entry.get('instructions', []):
                    program_idx = inner_ix.get('programIdIndex')
                    if program_idx is None or program_idx >= len(full_account_keys):
                        continue
                    if full_account_keys[program_idx] != MPL_CORE_PROGRAM:
                        continue

                    raw_data = inner_ix.get('data')
                    if not raw_data:
                        continue
                    try:
                        ix_data = base58.b58decode(raw_data)
                    except Exception:
                        nft_logger.debug(f"Could not base58-decode inner instruction data sig={tx_signature}")
                        continue

                    await maybe_handle_create(ix_data, inner_ix.get('accounts', []), "CPI")

        if mpl_core_mentioned and not mpl_core_top_level:
            nft_logger.debug(f"mpl-core mentioned but not a top-level instruction (CPI) sig={tx_signature}")

    except Exception as e:
        nft_logger.error(f"Error processing transaction: {str(e)}")
        import traceback
        nft_logger.error(traceback.format_exc())


async def listen_for_new_collections():
    """Listen for mpl-core collection creation with improved websocket handling"""

    while True:  # Outer reconnection loop
        try:
            async with websockets.connect(
                WSS_MOMENTUM_ENDPOINT,
                ping_interval=15,
                ping_timeout=10,
                close_timeout=5
            ) as websocket:
                nft_logger.info("WebSocket connected successfully")

                subscription_message = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "blockSubscribe",
                    "params": [
                        {"mentionsAccountOrProgram": MPL_CORE_PROGRAM},
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
                nft_logger.info(f"🔍 Subscribed to mpl-core program: {MPL_CORE_PROGRAM}")

                while True:  # Inner message processing loop
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=30)

                        # Add validation before parsing
                        if not response or not response.strip():
                            nft_logger.warning("Received empty response, skipping...")
                            continue

                        data = json.loads(response)

                        if 'error' in data:
                            error_code = data['error'].get('code')
                            error_message = data['error'].get('message', '')

                            if error_code == -32005 or 'RPS limit' in error_message:
                                # Extract wait time from the error
                                try_again_in = data['error'].get('data', {}).get('try_again_in', '100ms')
                                nft_logger.error(f"🚫 Rate limited! Waiting {try_again_in} before retry")

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

                                    nft_logger.info(f"⏳ Sleeping for {wait_seconds:.3f} seconds")
                                    await asyncio.sleep(wait_seconds + 0.1)  # Add 100ms buffer
                                else:
                                    # Fallback if parsing fails
                                    nft_logger.warning("Could not parse wait time, using default 200ms")
                                    await asyncio.sleep(0.2)

                                # Force reconnection to retry subscription
                                nft_logger.info("🔄 Retrying subscription after rate limit")
                                break
                            else:
                                nft_logger.error(f"RPC Error: {error_message}")
                                continue

                        # Message type handling
                        message_type = data.get('method', 'Unknown')

                        if message_type == 'blockNotification':
                            if 'params' in data and 'result' in data['params']:
                                block_data = data['params']['result']
                                if 'value' in block_data and 'block' in block_data['value']:
                                    block = block_data['value']['block']
                                    if 'transactions' in block:
                                        nft_logger.debug(f"📦 Block notification: {len(block['transactions'])} matching transaction(s)")
                                        for tx in block['transactions']:
                                            if isinstance(tx, dict) and 'transaction' in tx:
                                                tx_data_decoded = base64.b64decode(tx['transaction'][0])
                                                await process_transaction(tx_data_decoded, tx.get('meta'))

                        elif 'result' in data:
                            nft_logger.info("✅ NFT collection subscription confirmed")
                        else:
                            nft_logger.warning(f"❓ Unhandled message type: {message_type}")
                            nft_logger.debug(f"Full message: {data}")

                    except asyncio.TimeoutError:
                        if not websocket.open:
                            nft_logger.info("Connection lost during timeout, triggering reconnect")
                            break  # Break inner loop to reconnect

                        try:
                            pong = await websocket.ping()
                            await asyncio.wait_for(pong, timeout=5)
                            nft_logger.debug("Ping successful")
                        except Exception as ping_error:
                            nft_logger.warning(f"Ping failed: {ping_error}, triggering reconnect")
                            break  # Break inner loop to reconnect

                    except json.JSONDecodeError as json_error:
                        nft_logger.error(f"JSON decode error: {json_error}")
                        continue  # Skip this message, continue processing

                    except Exception as msg_error:
                        nft_logger.error(f"Message processing error: {msg_error}")

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
                            nft_logger.error("WebSocket connection unstable, forcing reconnection")
                            break  # Exit inner loop to trigger reconnection
                        continue  # Skip this message, continue processing

        except websockets.exceptions.ConnectionClosedError as conn_error:
            nft_logger.error(f"WebSocket connection closed: {conn_error}")
            nft_logger.info("Attempting reconnection in 5 seconds...")
            await asyncio.sleep(5)

        except Exception as e:
            nft_logger.error(f"Unexpected connection error: {e}")
            nft_logger.info("Attempting reconnection in 5 seconds...")
