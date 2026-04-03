import asyncio
import json
import os
import time
import logging
from logging.handlers import RotatingFileHandler
import traceback
from collections import defaultdict
import datetime
import discord
import struct
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from utils import format_social_link, get_saved_bonk_metadata, get_top_holders, safe_json_read, safe_json_write, safe_file_move, safe_file_exists, safe_file_delete
from rate_limiter import queue_discord_send, MessagePriority, safe_rpc_call
from telegram_sender import queue_telegram_send, get_telegram_targets
from telegram_formatter import format_bonk_bonding_curve
import sys

sys.path.append('/home/shaolin_saga/config')
from config import RPC_BONDING_ENDPOINT, SS_ICON_URL, LAMPORTS_PER_SOL

# Global variables
bot = None
servers = None

# Constants
BONK_BATCH_SIZE = 10
BATCH_DELAY = 2
processing_bonk_curves = False

# Bonding curve constants for Bonk.fun (Raydium launchpad)
TOKEN_DECIMALS = 9  # Bonk.fun tokens use 9 decimals
BONK_TOTAL_SUPPLY = 1_000_000_000  # 1 billion tokens

# Set up logger
def setup_logger(name, log_file):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    file_handler = RotatingFileHandler(f'/home/shaolin_saga/logs/{log_file}', maxBytes=10485760, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    return logger

bonk_bonding_logger = setup_logger('bonk_bonding_curve', 'bonk_bonding_curve.log')

# Emoji constants
green_tick = "\u2705"
red_cross = "\u274C"

def has_dexscreener_data(token_mint):
    """Check if DexScreener data exists for the given mint"""
    filename = f"/home/shaolin_saga/data/dex_data/dexscreener/{token_mint}.json"
    return os.path.exists(filename)

def decode_bonding_curve_data(hex_data: str) -> dict:
    """
    Decode Raydium pool account data to get token balance.
    Reads from Pool 1 account which contains the remaining tokens at byte offset 64.
    
    Args:
        hex_data (str): Hex string of the pool account data
        
    Returns:
        Dict containing token balance and progress information
    """
    try:
        # Convert hex to bytes
        data = bytes.fromhex(hex_data)
        
        # For Raydium pool accounts, the token balance is at byte offset 64
        token_balance_offset = 64
        token_balance_raw = struct.unpack('<Q', data[token_balance_offset:token_balance_offset+8])[0]
        
        # Convert to human readable tokens 
        tokens_remaining = token_balance_raw / 1e6
        if tokens_remaining > BONK_TOTAL_SUPPLY:
            tokens_remaining = token_balance_raw / 1e9
        
        # Calculate progress
        # Progress = (tokens_sold / total_supply) * 100
        # tokens_sold = total_supply - tokens_remaining
        tokens_sold = BONK_TOTAL_SUPPLY - tokens_remaining
        progress_percentage = (tokens_sold / BONK_TOTAL_SUPPLY) * 100
        progress_percentage = max(0, min(100, progress_percentage))
        
        
        return {
            "success": True,
            "platform": "Bonk.fun (Raydium Launchpad)",
            "raw_data": {
                "token_balance_raw": token_balance_raw,
                "tokens_remaining": tokens_remaining
            },
            "calculated_values": {
                "progress_percentage": progress_percentage,
                "tokens_remaining": tokens_remaining,
                "tokens_sold": tokens_sold,
                "is_complete": progress_percentage >= 100
            }
        }
        
    except Exception as e:
        bonk_bonding_logger.error(f"Failed to decode bonding curve data: {str(e)}")
        return {"error": f"Failed to decode: {str(e)}", "success": False}


async def get_bonding_curve_data(client, bonding_curve_address):
    """
    Get bonding curve data from the bonding curve (pool) account address.
    """
    try:
        bonk_bonding_logger.debug(f"🔍 Getting bonding curve data for: {bonding_curve_address}")
        
        # Get the bonding curve account data
        response = await safe_rpc_call(
            client.get_account_info,
            Pubkey.from_string(bonding_curve_address),
            max_retries=3,
            logger=bonk_bonding_logger
        )
        
        if not response.value or not response.value.data:
            bonk_bonding_logger.warning(f"No data for bonding curve account {bonding_curve_address}")
            return None
            
        # Convert account data to hex and decode
        hex_data = response.value.data.hex()
        curve_data = decode_bonding_curve_data(hex_data)
        
        if not curve_data.get("success"):
            bonk_bonding_logger.error(f"Failed to decode bonding curve data: {curve_data.get('error')}")
            return None
            
        return curve_data
        
    except Exception as e:
        bonk_bonding_logger.error(f"Error getting bonding curve data for {bonding_curve_address}: {e}")
        return None


def save_bonk_bonding_curve_state(curve_data, token_mint, directory, notification_status=None):
    """Save bonk bonding curve state with notification status"""
    os.makedirs(f'/home/shaolin_saga/data/bonk_data/bonk_{directory}', exist_ok=True)
    filename = f"/home/shaolin_saga/data/bonk_data/bonk_{directory}/{token_mint}.json"

    # Create the data to save
    data = curve_data.copy() if curve_data else {}

    # Add notification status if provided
    if notification_status:
        data.update(notification_status)
    elif os.path.exists(filename):
        # Preserve existing notification status if file exists
        try:
            with open(filename, 'r') as f:
                existing_data = json.load(f)
            # Extract notification fields from existing data
            notification_fields = {k: v for k, v in existing_data.items()
                                 if k.startswith('notified_')}
            data.update(notification_fields)

            # Also preserve bonding curve address if it exists
            if 'bondingCurve' in existing_data:
                data['bondingCurve'] = existing_data['bondingCurve']

        except Exception as e:
            bonk_bonding_logger.error(f"Error reading existing notification status: {str(e)}")

    # Save the updated data
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

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

async def process_bonk_bonding_curve(bonding_curve_address, token_mint):
    """Process a single bonk bonding curve using bonding curve account"""
    async with AsyncClient(RPC_BONDING_ENDPOINT) as client:
        try:
            curve_data = await get_bonding_curve_data(client, bonding_curve_address)
            #bonk_bonding_logger.debug(f"Curve data from process_bonk_bonding: {curve_data}")
            if not curve_data:
                bonk_bonding_logger.info(f"Bonding curve account {bonding_curve_address} no longer exists, removing files")
                
                # Remove from all possible directories
                directories = [
                    '/home/shaolin_saga/data/bonk_data/active_bonk_tokens',
                    '/home/shaolin_saga/data/bonk_data/bonk_under80',
                    '/home/shaolin_saga/data/bonk_data/bonk_80percent',
                    '/home/shaolin_saga/data/bonk_data/bonk_95percent'
                ]
                
                for directory in directories:
                    file_path = f"{directory}/{token_mint}.json"
                    if safe_file_exists(file_path) and safe_file_delete(file_path, logger=bonk_bonding_logger):
                        bonk_bonding_logger.info(f"Removed dead bonk token file: {file_path}")
                return

        except Exception as e:
            bonk_bonding_logger.error(f"Error processing bonk curve {bonding_curve_address}: {e}")
            return

        calc_values = curve_data["calculated_values"]
        actual_progress = calc_values['progress_percentage']
        
        bonk_bonding_logger.debug(f"🔍 {token_mint} progress: {actual_progress:.2f}%, tokens remaining: {calc_values['tokens_remaining']:,.0f}")
        
        creator_info = None

        # Initialize notification status
        notification_status = {}

        # Use actual_progress instead of progress for threshold checks
        if actual_progress >= 80 and actual_progress < 95:
            bonk_bonding_logger.info(f"🟡 Bonk {token_mint} hit 80% threshold! ({actual_progress:.1f}%)")
            bonk_bonding_logger.info(f"   Tokens remaining: {calc_values['tokens_remaining']:,.0f}")
            bonk_bonding_logger.info(f"   Tokens sold: {calc_values['tokens_sold']:,.0f}")
            
            # Check if we've already notified for 80%
            file_path = f"/home/shaolin_saga/data/bonk_data/bonk_80percent/{token_mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonk_bonding_logger)
            already_notified = existing_data.get('notified_80percent', False)
            

            notification_status['notified_80percent'] = True
            notification_status['bondingCurve'] = str(bonding_curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = token_mint
            notification_status['progress'] = actual_progress  # Use actual_progress
            
            save_bonk_bonding_curve_state(curve_data, token_mint, "80percent", notification_status)

            if not already_notified:
                await trigger_bonk_discord_alert(bonding_curve_address, token_mint, "80percent", actual_progress, calc_values)

        elif actual_progress >= 95 and actual_progress < 100:
            bonk_bonding_logger.info(f"🟠 Bonk {token_mint} hit 95% threshold! ({actual_progress:.1f}%)")
            bonk_bonding_logger.info(f"   Tokens remaining: {calc_values['tokens_remaining']:,.0f}")
            bonk_bonding_logger.info(f"   Tokens sold: {calc_values['tokens_sold']:,.0f}")
            
            # Check if we've already notified for 95%
            file_path = f"/home/shaolin_saga/data/bonk_data/bonk_95percent/{token_mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonk_bonding_logger)
            already_notified = existing_data.get('notified_95percent', False)

            notification_status['notified_95percent'] = True
            notification_status['bondingCurve'] = str(bonding_curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = token_mint
            notification_status['progress'] = actual_progress  # Use actual_progress
            
            save_bonk_bonding_curve_state(curve_data, token_mint, "95percent", notification_status)
            if not already_notified:
                await trigger_bonk_discord_alert(bonding_curve_address, token_mint, "95percent", actual_progress, calc_values)

        elif actual_progress >= 100 or calc_values.get('is_complete', False):
            bonk_bonding_logger.info(f"🔴 Bonk {token_mint} completed bonding curve! ({actual_progress:.1f}%)")
            bonk_bonding_logger.info(f"   Final tokens remaining: {calc_values['tokens_remaining']:,.0f}")
            bonk_bonding_logger.info(f"   Total tokens sold: {calc_values['tokens_sold']:,.0f}")
            
            # Check if we've already notified for completion
            file_path = f"/home/shaolin_saga/data/bonk_data/bonk_bondingComplete/{token_mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonk_bonding_logger)
            already_notified = existing_data.get('notified_complete', False)


            notification_status['notified_complete'] = True
            notification_status['bondingCurve'] = str(bonding_curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = token_mint
            notification_status['progress'] = actual_progress  # Use actual_progress
            
            save_bonk_bonding_curve_state(curve_data, token_mint, "bondingComplete", notification_status)
            
            if not already_notified:
                await trigger_bonk_discord_alert(bonding_curve_address, token_mint, "complete", actual_progress, calc_values)

        else:
            bonk_bonding_logger.debug(f"⚪ Bonk {token_mint} still under 80% ({actual_progress:.1f}%)")
            bonk_bonding_logger.debug(f"   Tokens remaining: {calc_values['tokens_remaining']:,.0f}")
            bonk_bonding_logger.debug(f"   Tokens sold: {calc_values['tokens_sold']:,.0f}")
            
            # Save state for under80
            notification_status['bondingCurve'] = str(bonding_curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = token_mint
            notification_status['progress'] = actual_progress  # Use actual_progress
            
            save_bonk_bonding_curve_state(curve_data, token_mint, "under80", notification_status)


async def trigger_bonk_discord_alert(bonding_curve_address, token_mint, stage, actual_progress, calc_values):
    """Send bonk bonding curve alert to Discord"""

    try:
        from rate_limiter import get_message_queue
        queue = get_message_queue()
    except Exception as e:
        bonk_bonding_logger.error(f"🔍 DEBUG: Failed to get message queue: {e}")
        return

    embed = await create_bonk_bonding_embed(bonding_curve_address, token_mint, stage, actual_progress, calc_values)
    
    for server in servers['allowed_servers']:
        server_id = server['server_id']
        
        if stage == "80percent":
            channel = get_channel(server_id, 'bonk_80_bonding_curve')
            bonk_bonding_logger.info(f'Attempting to send bonk embed for: {stage}')
        elif stage == "95percent":
            channel = get_channel(server_id, 'bonk_95_bonding_curve')
            bonk_bonding_logger.info(f'Attempting to send bonk embed for: {stage}')
        elif stage == "complete":
            channel = get_channel(server_id, 'bonk_bonding_curve_completed')
            bonk_bonding_logger.info(f'Attempting to send bonk embed for: {stage}')
        
        if channel:
            bonk_bonding_logger.info(f'Attempting to send bonk embed to channel: {channel.id}')
            try:
                await queue_discord_send(channel, embed, "bonk_bonding", bonk_bonding_logger, MessagePriority.HIGH)
            except Exception as e:
                bonk_bonding_logger.error(f"Error sending bonk embed: {str(e)}")
                bonk_bonding_logger.error(f"Full traceback: {traceback.format_exc()}")

    if stage in ("80percent", "complete"):
        tg_signal = "80_bonk_bonding" if stage == "80percent" else "bonk_bonding_completed"
        token_data = await get_saved_bonk_metadata(token_mint)
        if token_data:
            creator = token_data.get('account_mapping', {}).get('creator')
            tg_text = format_bonk_bonding_curve(
                token_mint, stage,
                token_data.get('name', 'Unknown'), token_data.get('symbol', '?'),
                creator, token_data.get('twitter_url'), token_data.get('telegram_url'),
                token_data.get('website_url'), actual_progress
            )
            for target in get_telegram_targets(tg_signal):
                await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, tg_signal, bonk_bonding_logger)

async def create_bonk_bonding_embed(bonding_curve_address, token_mint, stage, actual_progress, calc_values):
    """Create Discord embed for bonk bonding curve alerts"""
    # Get token data
    token_data = await get_saved_bonk_metadata(token_mint)
    
    if token_data:
        token_name = token_data.get('name', 'Unknown')
        token_symbol = token_data.get('symbol', 'Unknown')
        image_url = token_data.get('image_url')
        description = token_data.get('description') or 'No Description Added'
        website_url = token_data.get('website_url')
        twitter_url = token_data.get('twitter_url')
        telegram_url = token_data.get('telegram_url')
        user = token_data.get('account_mapping', {}).get('creator')
    else:
        token_name = 'Unknown'
        token_symbol = 'Unknown'
        image_url = None
        description = 'No Description Added'
        twitter_url = None
        telegram_url = None
        website_url = None
        user = None
    
    contract_uri = f'https://bonk.fun/token/{token_mint}'
    creator_uri = 'https://solscan.io/account/' + (user or 'Unknown')

    # Set stage-specific text
    if stage == "80percent":
        stage_text = "80 Percent Reached!"
    elif stage == "95percent":
        stage_text = "95 Percent Reached!"
    elif stage == "complete":
        stage_text = "Bonding Complete - Trading on Raydium!"
    else:
        stage_text = stage

    embed = discord.Embed(
        title=f"{token_name} ({token_symbol})",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )
    
    embed.set_thumbnail(url=image_url)
    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

    embed.add_field(name="Bonk Bonding Curve Alert", value=f'```{stage_text}```', inline=False)
    
    # Progress bar visualization
    #progress_bar_length = 20
    #filled_length = int(progress_bar_length * progress / 100)
    #progress_bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
    
    #embed.add_field(
    #    name="🎯 Progress", 
    #    value=f"```{progress_bar} {progress:.1f}%```", 
    #    inline=False
    #)
    
    # Add bonding curve metrics
    #if calc_values:
    #    embed.add_field(name="🪙 Tokens Remaining", value=f"```{calc_values['tokens_remaining']:,.0f}```", inline=True)
    #    embed.add_field(name="🔥 Tokens Sold", value=f"```{calc_values['tokens_sold']:,.0f}```", inline=True)
    #    embed.add_field(name="📊 Progress", value=f"```{progress:.2f}%```", inline=True)
    
    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{token_mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)


    if len(description) > 1000:
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)
   
    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

    # Add DexScreener indicator
    dex_status = green_tick if has_dexscreener_data(token_mint) else red_cross
    embed.add_field(name="Dex Paid?", value=dex_status, inline=True)
    
    # Get top holders
    async with AsyncClient(RPC_BONDING_ENDPOINT) as client:
        top_holders = await get_top_holders(client, Pubkey.from_string(token_mint), logger=bonk_bonding_logger)
        if top_holders:
            embed.add_field(name="Top Holders", value=f'```{top_holders}```', inline=False)

    # Hotkeys section
    hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_mint}) | "
        f"[ AXIOM ](https://axiom.trade/t/{token_mint}/@codesaga) | "
        f"[ PADRE ](https://trade.padre.gg/trade/solana/{token_mint}?rk=shaolinsaga) | "
        f"[ DEX ](https://dexscreener.com/solana/{token_mint}) | "
        f"[ BONK ](https://bonk.fun/token/{token_mint}) "
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

    return embed

async def process_all_bonk_curves():
    """Process all bonk bonding curves"""
    global processing_bonk_curves
    
    if processing_bonk_curves:
        bonk_bonding_logger.warning("⚠️ Previous bonk curve processing still running, skipping this cycle")
        return
    
    processing_bonk_curves = True
    start_time = time.time()
    
    try:
        bonk_bonding_logger.debug("Starting to process all bonk curves")
        
        directories = [
            '/home/shaolin_saga/data/bonk_data/active_bonk_tokens',  # New bonk tokens
            '/home/shaolin_saga/data/bonk_data/bonk_under80',        # Tokens under 80%
            '/home/shaolin_saga/data/bonk_data/bonk_80percent',      # Tokens at 80%+
            '/home/shaolin_saga/data/bonk_data/bonk_95percent'       # Tokens at 95%+
        ]
        
        total_tokens = 0
        
        for directory in directories:
            if not os.path.exists(directory):
                bonk_bonding_logger.warning(f"Directory does not exist: {directory}")
                continue
                
            dir_token_count = len(os.listdir(directory))
            total_tokens += dir_token_count
            bonk_bonding_logger.debug(f"Processing bonk directory: {directory} ({dir_token_count} tokens)")
            
            # Batch processing
            batch = []
            
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            data = json.load(f)
                        
                        token_mint = filename.replace('.json', '')
                        bonding_curve_address = data.get('bondingCurve')
                        
                        if not bonding_curve_address:
                            bonk_bonding_logger.warning(f"No bondingCurve found for {token_mint}")
                            continue
                        
                        # Add to batch
                        batch.append((bonding_curve_address, token_mint))
                        
                        # Process batch when it reaches batch size
                        if len(batch) >= BONK_BATCH_SIZE:
                            bonk_bonding_logger.debug(f"Processing bonk batch of {len(batch)} tokens")
                            tasks = [process_bonk_bonding_curve(addr, mint) for addr, mint in batch]
                            await asyncio.gather(*tasks, return_exceptions=True)
                            batch = []
                            await asyncio.sleep(BATCH_DELAY)
                            
                    except Exception as e:
                        bonk_bonding_logger.error(f"Error loading bonk file {filename}: {str(e)}")
                        continue
            
            # Process remaining items in batch
            if batch:
                bonk_bonding_logger.debug(f"Processing final bonk batch of {len(batch)} tokens")
                tasks = [process_bonk_bonding_curve(addr, mint) for addr, mint in batch]
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(BATCH_DELAY)
        
        bonk_bonding_logger.debug("Finished processing all bonk curves")
        
    except Exception as e:
        bonk_bonding_logger.error(f"Critical error in process_all_bonk_curves: {str(e)}")
        bonk_bonding_logger.error(f"Full traceback: {traceback.format_exc()}")
        
    finally:
        processing_bonk_curves = False
        duration = time.time() - start_time
        bonk_bonding_logger.info(f"✅ Completed processing {total_tokens} bonk curves in {duration:.2f} seconds")
        
        if total_tokens > 0:
            avg_time = duration / total_tokens
            bonk_bonding_logger.info(f"📊 Average: {avg_time:.2f}s per bonk token")

async def bonk_bonding_curve_monitor():
    """Monitor bonk bonding curves - runs continuously"""
    bonk_bonding_logger.info("Starting bonk bonding curve monitor")
    
    while True:
        bonk_bonding_logger.info("🌅 Starting next bonk bonding curve cycle")
        cycle_start = time.time()
        
        try:
            await process_all_bonk_curves()
            bonk_bonding_logger.info("✅ Bonk bonding curve processing finished, ready for next cycle")
            
        except Exception as e:
            bonk_bonding_logger.error(f"Error in bonk bonding curve monitor: {e}")
            bonk_bonding_logger.error(f"Full traceback: {traceback.format_exc()}")
            processing_bonk_curves = False  # Failsafe: always clear the flag in case of error
            
        cycle_duration = time.time() - cycle_start
        bonk_bonding_logger.info(f"😴 Bonk cycle complete, duration: {cycle_duration:.2f}s. Brief pause before next cycle...")
        await asyncio.sleep(90)

async def start_monitoring(bot_instance, servers_config):
    """Start bonk bonding curve monitoring"""
    global bot, servers
    bot = bot_instance
    servers = servers_config

    bonk_bonding_logger.info("Starting Bonk bonding curve monitoring")
    bonk_bonding_logger.info(f"Using bot: {bot.user}")

    # Make sure the required directories exist
    os.makedirs("/home/shaolin_saga/data/bonk_data/active_bonk_tokens", exist_ok=True)
    os.makedirs("/home/shaolin_saga/data/bonk_data/bonk_under80", exist_ok=True)
    os.makedirs("/home/shaolin_saga/data/bonk_data/bonk_80percent", exist_ok=True)
    os.makedirs("/home/shaolin_saga/data/bonk_data/bonk_95percent", exist_ok=True)
    os.makedirs("/home/shaolin_saga/data/bonk_data/bonk_bondingComplete", exist_ok=True)

    # Start the monitoring task
    bot.loop.create_task(bonk_bonding_curve_monitor())

    bonk_bonding_logger.info("Bonk bonding curve monitoring started successfully")
