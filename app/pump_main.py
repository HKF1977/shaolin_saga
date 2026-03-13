import datetime
import traceback
import logging
from logging.handlers import RotatingFileHandler
from io import BytesIO
import threading
import time
import asyncio
import websockets
import json
import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import requests
import base64
import base58
import struct
import hashlib
import sys
import aiohttp
import re
import bonk_main
import bonk_bonding_monitor
from collections import defaultdict
from utils import analyze_for_bundling, get_token_transactions, get_contract_uri_for_mint, get_top_holders, get_saved_bonk_metadata, get_saved_transaction_metadata, safe_json_read, safe_json_write, safe_file_move, safe_file_exists, safe_file_delete
from pump_commands import register_commands
from typing import Tuple, Optional
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from typing import Final, List, Tuple
from construct import Struct, Int64ul, Flag
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from collections import deque
from time import time as current_time

# At the top of your main files:
from rate_limiter import queue_discord_send, MessagePriority, get_message_queue, monitor_rate_limits, safe_rpc_call, log_rpc_stats, get_rpc_rate_limiter, ensure_queue_processing, monitor_memory_usage
from telegram_sender import queue_telegram_send, get_telegram_targets, ensure_telegram_queue_processing
from telegram_formatter import format_all_tokens, format_new_coin_with_socials, format_bonding_curve, format_trade_signal, format_pump_livestream, format_dexscreener_boost, format_dexscreener_paid

#Get vars from config.py
#sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append('/home/shaolin_saga/config')
from config import  WSS_MOMENTUM_ENDPOINT, RPC_MOMENTUM_ENDPOINT, RPC_BONDING_ENDPOINT, WSS_BONDING_ENDPOINT, WSS_ENDPOINT, PUMP_PROGRAM, RPC_ENDPOINT, SYSTEM_TOKEN_PROGRAM, RAYDIUM_AMM_PROGRAM, SS_ICON_URL, PUMP_LS_URL


#Constants
#CURVE_BATCH_SIZE = 10
CURVE_BATCH_SIZE = 20
#BATCH_DELAY = 2
BATCH_DELAY = 1
processing_curves = False
processing_momentum = False

def load_idl(file_path):
    with open(file_path, 'r') as f:
        return json.load(f)

# Load environment secrets
load_dotenv()

# Load allowed servers
with open('/home/shaolin_saga/config/servers.json', 'r') as server_file:
    servers = json.load(server_file)

# Initialize the bot with a command prefix and intents
intents = discord.Intents.default()
intents.typing = False
intents.presences = False
bot = commands.Bot(command_prefix='!', intents=intents)

# Set up the loggers
def setup_logger(name, log_file):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    file_handler = RotatingFileHandler(f'/home/shaolin_saga/logs/{log_file}', maxBytes=10485760, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    return logger

# Create separate loggers
bonding_logger = setup_logger('bonding_curve', 'bonding_curve.log')
websocket_logger = setup_logger('websocket', 'websocket.log')
dexscreener_logger = setup_logger('dexscreener_monitor', 'dexscreener_monitor.log')
command_logger = setup_logger('commands', 'commands.log')
momentum_logger = setup_logger('momentum', 'momentum.log')
livestream_logger = setup_logger('livestream_monitor', 'livestream_monitor.log')
rpc_logger = setup_logger('rpc_monitor', 'rpc_monitor.log')

# Register commands with the bot
register_commands(bot, command_logger)

#Custom JSON Encoder
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, BytesIO):
            return base64.b64encode(obj.getvalue()).decode('utf-8')
        return super().default(obj)

#Setup RPC monitor
class RPCMonitor:
    def __init__(self, window_size=1):
        self.requests = deque()
        self.window_size = window_size

    def add_request(self):
        now = current_time()
        self.requests.append(now)
        
        while self.requests and self.requests[0] < now - self.window_size:
            self.requests.popleft()
    def get_current_rate(self):
        return len(self.requests)

# Create monitor instance
#rpc_monitor = RPCMonitor()

# Function to get channel for a specific server
#def get_channel(server_id, channel_type):
#    for server in servers['allowed_servers']:
#        if server['server_id'] == server_id:
#            channel = bot.get_channel(server['channels'][channel_type])
#            #print(f'{channel} from get channel')
#            return channel
#    return None

def get_channel(server_id, channel_type):
    for server in servers['allowed_servers']:
        if server['server_id'] == server_id:
            channel_id = server['channels'].get(channel_type)  # Returns None if missing
            if channel_id:
                return bot.get_channel(channel_id)
    return None

'''
def get_contract_uri_for_mint(mint: str) -> Optional[str]:
    """
    Returns the contract URI for a mint containing 'bonk' or 'pump'.
    
    """
    if "bonk" in mint:
        return f"https://bonk.fun/token/{mint}"
    elif "pump" in mint:
        return f"https://pump.fun/{mint}"
    else:
        return "Unknown"
'''

def save_active_token(data):
    """Save minimal data for bonding curve monitoring with safe file operations"""
    filename = f"/home/shaolin_saga/data/pump_data/active_tokens/{data['mint']}.json"

    active_data = {
        'mint': data['mint'],
        'bondingCurve': data['bondingCurve'],
        'associatedBondingCurve': data['associatedBondingCurve'],
        'user': data['user'],
        'created': time.time()
    }

    if safe_json_write(filename, active_data, logger=websocket_logger):
        websocket_logger.debug(f"Successfully saved active token data for {data['mint']}")
        return True
    else:
        websocket_logger.error(f"Failed to save active token data for {data['mint']}")
        return False


def save_token_metadata(mint, enriched_data):
    """Save processed metadata for embeds with safe file operations"""
    filename = f"/home/shaolin_saga/data/pump_data/metadata/{mint}.json"
    
    if safe_json_write(filename, enriched_data, logger=websocket_logger):
        websocket_logger.debug(f"Successfully saved metadata for {mint}")
        return True
    else:
        websocket_logger.error(f"Failed to save metadata for {mint}")
        return False


async def fetch_pump_livestreams():
    """Fetch currently live streams from pump.fun API"""
    url = PUMP_LS_URL    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    livestream_logger.info(f"Successfully fetched livestreams: {len(data)} streams found")
                    return data
                else:
                    livestream_logger.error(f"Failed to fetch livestreams: {response.status} - {await response.text()}")
                    return None
    except Exception as e:
        livestream_logger.error(f"Error fetching livestreams: {str(e)}")
        return None

async def process_pump_livestreams():
    """Process pump.fun livestreams and detect new ones"""
    data = await fetch_pump_livestreams()
    if not data:
        livestream_logger.error("Failed to fetch livestreams")
        return

    livestream_logger.info(f"Processing {len(data)} livestreams")

    # Create directory if it doesn't exist
    os.makedirs('/home/shaolin_saga/data/pump_data/pump_livestreams', exist_ok=True)

    # Process each livestream
    for stream in data:
        mint = stream.get('mint')
        if not mint:
            continue

        filename = f"/home/shaolin_saga/data/pump_data/pump_livestreams/{mint}.json"
        
        # Check if we've already processed this livestream
        is_new_stream = not os.path.exists(filename)
        
        # Save livestream data to file (always update the file with latest data)
        with open(filename, 'w') as f:
            json.dump(stream, f, indent=2)
        
        # Only trigger alert for new livestreams
        if is_new_stream:
            await trigger_livestream_alert(stream)

async def trigger_livestream_alert(stream_data):
    """Send new livestream alert to Discord"""
    embed = await create_livestream_embed(stream_data)
    
    for server in servers['allowed_servers']:
        if 'pump_livestreams' in server['channels']:  # Only try servers with the channel configured
            channel = get_channel(server['server_id'], 'pump_livestreams')
            if channel:
                await queue_discord_send(channel, embed, "pump_livestreams", websocket_logger, MessagePriority.MEDIUM)
                livestream_logger.info(f"Sent livestream alert for {stream_data.get('mint')} to server {server['server_id']}")
                await asyncio.sleep(1)

    # Telegram: pump_livestreams
    tg_text = format_pump_livestream(stream_data)
    for target in get_telegram_targets('pump_livestreams'):
        await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, 'pump_livestreams', websocket_logger)

async def create_livestream_embed(stream_data):
    """Create Discord embed for new livestream"""
    mint = stream_data.get('mint', 'Unknown')
    name = stream_data.get('name', 'Unknown')
    symbol = stream_data.get('symbol', 'Unknown')
    description = stream_data.get('description', 'No description available')
    creator = stream_data.get('creator', 'Unknown')
    market_cap = stream_data.get('market_cap', 0)
    
    # Get social links
    twitter = stream_data.get('twitter', '')
    telegram = stream_data.get('telegram', '')
    website = stream_data.get('website', '')
    
    # Create embed
    embed = discord.Embed(
        title=f"🔴 LIVE: {name} ({symbol})",
        color=0xFF0000,  # Red for live
        timestamp=datetime.datetime.utcnow()
    )
    
    # Add thumbnail if available
    if stream_data.get('image_uri'):
        embed.set_thumbnail(url=stream_data['image_uri'])
    
    # Add livestream thumbnail if available
    if stream_data.get('thumbnail'):
        embed.set_image(url=stream_data['thumbnail'])
    
    # Contract and creator info
    contract_uri = f'https://pump.fun/{mint}'
    creator_uri = f'https://solscan.io/account/{creator}'
    
    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{creator}```', inline=False)
    
    # Description (truncated if too long)
    if len(description) > 1000:
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)
    
    # Market cap
    embed.add_field(name="Market Cap", value=f'```${market_cap:,.2f}```', inline=True)
    
    # Live stream info
    if stream_data.get('is_currently_live'):
        embed.add_field(name="Status", value="🔴 **LIVE NOW**", inline=True)
    
    # Reply count if available
    if stream_data.get('reply_count'):
        embed.add_field(name="Chat Activity", value=f"{stream_data['reply_count']} messages", inline=True)
    
    # Social links
    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website, "Website"), inline=True)
    
    # Quick buy links
    hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | "
        f"[ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | "
        f"[ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | "
        f"[ PUMP LIVE ](https://pump.fun/{mint})"
    )
    embed.add_field(name="Quick Links", value=hotkeys, inline=False)
    
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
    return embed

def pump_livestream_monitor():
    """Monitor thread for pump.fun livestreams"""
    livestream_logger.info("Starting pump.fun livestream monitor")
    while True:
        try:
            future = asyncio.run_coroutine_threadsafe(process_pump_livestreams(), bot.loop)
            try:
                future.result(timeout=120)
            except asyncio.TimeoutError:
                livestream_logger.warning("process_pump_livestreams timed out")
            except Exception as e:
                livestream_logger.error("Error in process_pump_livestreams: %s", str(e))

            livestream_logger.debug("Pump livestream cycle complete, sleeping for 300 seconds")
            time.sleep(300)  # Check every 5 minutes

        except Exception as e:
            livestream_logger.error(f"Unexpected error in pump_livestream_monitor: {str(e)}")
            livestream_logger.error(f"Full traceback: {traceback.format_exc()}")
            time.sleep(60)  # Sleep for a minute before retrying

async def check_and_alert_if_clean(mint_address: str, servers_config: list, get_channel_func, logger=None):
    """
    Run bundle check and alert to trade signals channel if score < 30.
    Designed to be called independently when token hits 15% bonding.
    """
    logger.info(f"Attempting Bundle check for {mint_address}")
    try:
        async with AsyncClient(RPC_ENDPOINT) as client:
            # Run bundle check
            signatures = await get_token_transactions(client, mint_address)
            if not signatures:
                if logger:
                    logger.info(f"No transactions for bundle check: {mint_address}")
                return
            
            bundle_analysis = await analyze_for_bundling(client, signatures, mint_address)
            score = bundle_analysis['bundle_score']
            
            if logger:
                logger.info(f"Bundle check for {mint_address}: score={score}")
            
            bonding_logger.info(f"Bundle check for {mint_address}: score={score}")
            
            # Only proceed if score < 30 (clean)
            if score >= 36:
            #if score >= 90:
                if logger:
                    logger.info(f"Bundle score too high ({score}), skipping alert")
                return
            
            # Get metadata
            metadata = await get_saved_transaction_metadata(mint_address, logger)
            if not metadata:
                if logger:
                    logger.warning(f"No metadata for {mint_address}")
                return
            
            # Create embed
            embed = await create_clean_bundle_embed(mint_address, metadata, score, bundle_analysis)
            
            # Send to trade signals channel
            for server in servers_config['allowed_servers']:
                channel = get_channel_func(server['server_id'], 'trade_signals')
                if channel:
                    await queue_discord_send(channel, embed, "trade_signals", logger, MessagePriority.HIGH)
                    if logger:
                        logger.info(f"✅ Sent clean bundle alert for {mint_address} to server {server['server_id']}")

            # Telegram: trade_signals
            tg_text = format_trade_signal(mint_address, metadata, score)
            for target in get_telegram_targets('trade_signals'):
                await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, 'trade_signals', logger)
            
    except Exception as e:
        if logger:
            logger.error(f"Error in check_and_alert_if_clean: {e}")


async def create_clean_bundle_embed(mint: str, metadata: dict, score: int, bundle_analysis: dict):
    """Create embed for tokens with clean bundle check (score < 30)"""
    bonding_logger.debug("sending create clean bundle embed")
    
    name = metadata.get('name', 'Unknown')
    symbol = metadata.get('symbol', 'UNKNOWN')
    user = metadata.get('user', 'Unknown')
    description = metadata.get('description', 'No description')
    image_url = metadata.get('image_url')
    twitter_url = metadata.get('twitter_url')
    telegram_url = metadata.get('telegram_url')
    website_url = metadata.get('website_url')
    
    embed = discord.Embed(
        title=f"{name} ({symbol})",
        #description=f"15% bonding reached with low bundle score",
        color=0x00FF00,
        timestamp=datetime.datetime.utcnow()
    )
    
    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL)
    
    if image_url:
        embed.set_thumbnail(url=image_url)
    
    contract_uri = f'https://pump.fun/{mint}'
    creator_uri = f'https://solscan.io/account/{user}'
    alert_text = "Moonshot Potential!"
    
    embed.add_field(name="Shaolin Trade Alert", value=f'```{alert_text}```', inline=False)
    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)
    
    # Description
    if len(description) > 1000:
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)
   
    '''
    # Socials
    def format_social_link(url, platform):
        green_tick = "\u2705"
        red_cross = "\u274C"
        if url:
            return f"[{green_tick} {platform}]({url})"
        else:
            return f"{red_cross} {platform}"
    '''

    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)
    
    # Bundle score
    embed.add_field(name="Bundle Score", value=f"🟢 {score}/100 (Low Risk)", inline=True)
    
    # Wallet analysis
    wallet_analysis = bundle_analysis.get('wallet_analysis', {})
    embed.add_field(
        name="Wallet Analysis",
        value=f"New Wallets: {wallet_analysis.get('new_wallets', 0)}\nSimilar Buys: {wallet_analysis.get('similar_buys', 0)}",
        inline=True
    )
    
    '''
    # Quick buy links
    hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | "
        f"[ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | "
        f"[ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    '''

    hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False) 
    
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
    
    return embed

#Monitor Bonding curve
def bonding_curve_monitor():
    bonding_logger.info("Starting bonding curve monitor")
    global processing_curves
    while True:
        bonding_logger.info("🌅 Starting next bonding curve cycle")
        cycle_start = time.time()
        try:
            future = asyncio.run_coroutine_threadsafe(process_all_curves(), bot.loop)
            try:
                #future.result(timeout=600)
                future.result(timeout=800)
            except Exception as e:
                bonding_logger.critical(f"process_all_curves timed out or failed: {e}")
                processing_curves = False  # Failsafe: forcibly reset the flag
            wait_count = 0
            while processing_curves:
                bonding_logger.warning("⏳ Bonding curve processing still ongoing after timeout, waiting 30s")
                time.sleep(30)
                wait_count += 1
                if wait_count > 5:  # After ~2.5 minutes, force reset
                    bonding_logger.critical("Force-resetting processing_curves after extended wait")
                    processing_curves = False
                    break
            bonding_logger.info("✅ Bonding curve processing finished, ready for next cycle")
        except Exception as e:
            bonding_logger.error(f"Error in bonding curve monitor: {e}")
            processing_curves = False  # Failsafe: always clear the flag in case of error
        cycle_duration = time.time() - cycle_start
        bonding_logger.info(f"😴 Cycle complete, duration: {cycle_duration:.2f}s. Brief pause before next cycle...")
        time.sleep(30)


def save_bonding_curve_state(state, mint, directory, notification_status=None):
    """Save bonding curve state with notification status"""
    os.makedirs(f'/home/shaolin_saga/data/pump_data/{directory}', exist_ok=True)
    filename = f"/home/shaolin_saga/data/pump_data/{directory}/{mint}.json"

    # Create the data to save
    data = state.__dict__.copy()

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

            # Also preserve bondingCurve if it exists
            if 'bondingCurve' in existing_data:
                data['bondingCurve'] = existing_data['bondingCurve']

        except Exception as e:
            bonding_logger.error(f"Error reading existing notification status: {str(e)}")

    # Save the updated data
    with open(filename, 'w') as f:
        json.dump(data, f, cls=CustomJSONEncoder)


async def process_all_curves():
    global processing_curves
    
    if processing_curves:
        bonding_logger.warning("⚠️ Previous curve processing still running, skipping this cycle")
        return
    
    processing_curves = True
    start_time = time.time()
    
    try:
        bonding_logger.debug("Starting to process all curves")
        
        directories = [
            '/home/shaolin_saga/data/pump_data/active_tokens',  # New tokens
            '/home/shaolin_saga/data/pump_data/under15',        # Tokens under 15%
            '/home/shaolin_saga/data/pump_data/15percent',      # Tokens at 15%+
            '/home/shaolin_saga/data/pump_data/35percent',      # Tokens at 35%+
            '/home/shaolin_saga/data/pump_data/80percent',      # Tokens at 80%+
            '/home/shaolin_saga/data/pump_data/95percent'      # Tokens at 95%+
        
        ]
        
        total_tokens = 0
        
        for directory in directories:
            if not os.path.exists(directory):
                bonding_logger.warning(f"Directory does not exist: {directory}")
                continue
                
            dir_token_count = len(os.listdir(directory))
            total_tokens += dir_token_count
            bonding_logger.debug(f"Processing directory: {directory} ({dir_token_count} tokens)")
            
            # Batch processing
            batch = []
            
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r') as f:
                            data = json.load(f)
                        
                        mint = filename.replace('.json', '')
                        curve_address = Pubkey.from_string(data['bondingCurve'])
                        
                        # Add to batch
                        batch.append((curve_address, mint))
                        
                        # Process batch when it reaches batch size
                        if len(batch) >= CURVE_BATCH_SIZE:
                            bonding_logger.debug(f"Processing batch of {len(batch)} tokens")
                            tasks = [process_bonding_curve(addr, mint) for addr, mint in batch]
                            await asyncio.gather(*tasks, return_exceptions=True)
                            batch = []
                            await asyncio.sleep(BATCH_DELAY)
                            
                    except Exception as e:
                        bonding_logger.error(f"Error loading file {filename}: {str(e)}")
                        continue
            
            # Process remaining items in batch
            if batch:
                bonding_logger.debug(f"Processing final batch of {len(batch)} tokens")
                tasks = [process_bonding_curve(addr, mint) for addr, mint in batch]
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(BATCH_DELAY)
        
        bonding_logger.debug("Finished processing all curves")
        
    except Exception as e:
        bonding_logger.error(f"Critical error in process_all_curves: {str(e)}")
        bonding_logger.error(f"Full traceback: {traceback.format_exc()}")
        
    finally:
        processing_curves = False
        duration = time.time() - start_time
        bonding_logger.info(f"✅ Completed processing {total_tokens} curves in {duration:.2f} seconds")
        
        if total_tokens > 0:
            avg_time = duration / total_tokens
            bonding_logger.info(f"📊 Average: {avg_time:.2f}s per token")

async def process_bonding_curve(curve_address, mint):
    async with AsyncClient(RPC_BONDING_ENDPOINT) as client:
        try:
            state = await get_cached_bonding_curve_state(client, curve_address)
        except Exception as e:
            # If we can't get the curve state, the account probably doesn't exist
            bonding_logger.info(f"Curve {curve_address} no longer exists, removing files")
            
            # SAFE FILE REMOVAL - Remove from all possible directories
            directories = [
                '/home/shaolin_saga/data/pump_data/active_tokens',
                '/home/shaolin_saga/data/pump_data/under15',
                '/home/shaolin_saga/data/pump_data/15percent',
                '/home/shaolin_saga/data/pump_data/35percent',
                '/home/shaolin_saga/data/pump_data/80percent',
                '/home/shaolin_saga/data/pump_data/95percent'
            ]
            
            for directory in directories:
                file_path = f"{directory}/{mint}.json"
                if safe_file_delete(file_path, logger=bonding_logger):
                    bonding_logger.info(f"Removed dead token file: {file_path}")
            return

        real_token_reserves = state.real_token_reserves / 1e6
        total_supply = 800000000
        #threshold_15 = total_supply * 0.85
        threshold_15 = total_supply * 0.80
        threshold_35 = total_supply * 0.65
        threshold_80 = total_supply * 0.2
        threshold_95 = total_supply * 0.05
        threshold_complete = 0

        # Get creator info from new metadata structure
        metadata = await get_saved_transaction_metadata(mint, logger=bonding_logger)
        creator_info = metadata.get('user') if metadata else None
        three_socials = metadata.get('three_socials', False) if metadata else False       
        
        # Initialize notification status
        notification_status = {}

        if real_token_reserves <= threshold_15 and real_token_reserves > threshold_35:
            bonding_logger.info(f"🟡 {mint} hit 15% threshold!")
            
            # SAFE FILE READ - Check if we've already notified for 80%
            file_path = f"/home/shaolin_saga/data/pump_data/15percent/{mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonding_logger)
            already_notified = existing_data.get('notified_15percent', False)

            # Save state with updated notification status AND creator info
            notification_status['notified_15percent'] = True
            notification_status['bondingCurve'] = str(curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = mint
            
            save_bonding_curve_state(state, mint, "15percent", notification_status)

            # Only send notification if we haven't already
            if not already_notified:
                await trigger_discord_alert(curve_address, mint, "15percent")
                if three_socials:
                    asyncio.create_task(
                        check_and_alert_if_clean(mint, servers, get_channel, bonding_logger)
                    ) 
        elif real_token_reserves <= threshold_35 and real_token_reserves > threshold_80:
            bonding_logger.info(f"🟡 {mint} hit 35% threshold!")
            
            # SAFE FILE READ - Check if we've already notified for 80%
            file_path = f"/home/shaolin_saga/data/pump_data/35percent/{mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonding_logger)
            already_notified = existing_data.get('notified_35percent', False)

            # Save state with updated notification status AND creator info
            notification_status['notified_35percent'] = True
            notification_status['bondingCurve'] = str(curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = mint
            
            save_bonding_curve_state(state, mint, "35percent", notification_status)

            # Only send notification if we haven't already
            if not already_notified:
                await trigger_discord_alert(curve_address, mint, "35percent")
        
        elif real_token_reserves <= threshold_80 and real_token_reserves > threshold_95:
            bonding_logger.info(f"🟡 {mint} hit 80% threshold!")
            
            # SAFE FILE READ - Check if we've already notified for 80%
            file_path = f"/home/shaolin_saga/data/pump_data/80percent/{mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonding_logger)
            already_notified = existing_data.get('notified_80percent', False)

            # Save state with updated notification status AND creator info
            notification_status['notified_80percent'] = True
            notification_status['bondingCurve'] = str(curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = mint
            
            save_bonding_curve_state(state, mint, "80percent", notification_status)

            # Only send notification if we haven't already
            if not already_notified:
                await trigger_discord_alert(curve_address, mint, "80percent")

        elif real_token_reserves <= threshold_95 and real_token_reserves > threshold_complete:
            bonding_logger.info(f"🟠 {mint} hit 95% threshold!")
            
            # SAFE FILE READ - Check if we've already notified for 95%
            file_path = f"/home/shaolin_saga/data/pump_data/95percent/{mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonding_logger)
            already_notified = existing_data.get('notified_95percent', False)

            # Save state with updated notification status AND creator info
            notification_status['notified_95percent'] = True
            notification_status['bondingCurve'] = str(curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = mint
            
            save_bonding_curve_state(state, mint, "95percent", notification_status)

            # Only send notification if we haven't already
            if not already_notified:
                await trigger_discord_alert(curve_address, mint, "95percent")

        elif real_token_reserves == threshold_complete:
            bonding_logger.info(f"🔴 {mint} completed bonding curve!")
            
            # SAFE FILE READ - Check if we've already notified for completion
            file_path = f"/home/shaolin_saga/data/pump_data/bondingComplete/{mint}.json"
            existing_data = safe_json_read(file_path, default={}, logger=bonding_logger)
            already_notified = existing_data.get('notified_complete', False)

            # Save state with updated notification status AND creator info
            notification_status['notified_complete'] = True
            notification_status['bondingCurve'] = str(curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = mint
            
            save_bonding_curve_state(state, mint, "bondingComplete", notification_status)

            # Only send notification if we haven't already
            if not already_notified:
                await trigger_discord_alert(curve_address, mint, "complete")

        else:
            bonding_logger.debug(f"⚪ {mint} still under 15% ({real_token_reserves:,.0f} tokens left)")
            
            # Even for under80, save creator info for future reference
            notification_status['bondingCurve'] = str(curve_address)
            if creator_info:
                notification_status['creator'] = creator_info
            notification_status['mint'] = mint
            
            save_bonding_curve_state(state, mint, "under15", notification_status)

# Trigger bonding curve embed
async def trigger_discord_alert(bonding_curve, mint, stage):
    bonding_logger.info(f"🔍 DEBUG: trigger_discord_alert called for {mint}, stage: {stage}")

        # Check if we can get the message queue
    try:
        from rate_limiter import get_message_queue
        queue = get_message_queue()
        bonding_logger.info(f"🔍 DEBUG: Got message queue, current size: {queue.queue.qsize()}")
        bonding_logger.info(f"🔍 DEBUG: Queue is_processing: {queue.is_processing}")  # ADD THIS
    except Exception as e:
        bonding_logger.error(f"🔍 DEBUG: Failed to get message queue: {e}")
        return

    embed = await create_bonding_embed(bonding_curve, mint, stage)
    bonding_logger.error(f"🔍 DEBUG: Embed created for mint {mint}, stage {stage}")
    for server in servers['allowed_servers']:
        server_id = server['server_id']
        
        if stage == "15percent":
            channel = get_channel(server_id, '15_bonding_curve')
            bonding_logger.info(f'Attempting to send embed for: {stage}')
        elif stage == "35percent":
            channel = get_channel(server_id, '35_bonding_curve')
            bonding_logger.info(f'Attempting to send embed for: {stage}')
        elif stage == "80percent":
            channel = get_channel(server_id, '80_bonding_curve')
            bonding_logger.info(f'Attempting to send embed for: {stage}')
        elif stage == "95percent":
            channel = get_channel(server_id, '95_bonding_curve')
            bonding_logger.info(f'Attempting to send embed for: {stage}')
        elif stage == "complete":
            channel = get_channel(server_id, 'bonding_curve_completed')
            bonding_logger.info(f'Attempting to send embed for: {stage}')
        
        if channel:
            bonding_logger.info(f'Attempting to send embed to channel: {channel.id}')
            try:
                await queue_discord_send(channel, embed, "bonding", bonding_logger, MessagePriority.HIGH)
            except Exception as e:
                bonding_logger.error(f"Error sending embed: {str(e)}")
                bonding_logger.error(f"Full traceback: {traceback.format_exc()}")

    # Telegram: bonding curve milestones (80% and complete only)
    if stage == "80percent":
        tg_signal = "80_bonding_curve"
    elif stage == "complete":
        tg_signal = "bonding_curve_completed"
    else:
        tg_signal = None

    if tg_signal:
        tg_targets = get_telegram_targets(tg_signal)
        if tg_targets:
            tx_data = await get_saved_transaction_metadata(mint, bonding_logger)
            tg_text = format_bonding_curve(mint, stage, tx_data)
            for target in tg_targets:
                await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, tg_signal, bonding_logger)

# Global momentum cache
momentum_cache = {}
MOMENTUM_CACHE_TTL = 120  # 2 minutes

# Track processed tokens to avoid spam
momentum_alerts_sent = defaultdict(set)

async def get_cached_bonding_curve_state(client, curve_address):
    """Cache bonding curve states to reduce RPC calls"""
    cache_key = str(curve_address)
    now = time.time()
    
    # Check cache first
    if cache_key in momentum_cache:
        cached_data, timestamp = momentum_cache[cache_key]
        if now - timestamp < MOMENTUM_CACHE_TTL:
            return cached_data
    
    # Fetch fresh data
    try:
        state = await get_bonding_curve_state(client, curve_address)
        momentum_cache[cache_key] = (state, now)
        return state
    except Exception as e:
        # If failed, use cached data if available
        if cache_key in momentum_cache:
            momentum_logger.warning(f"Using cached data for {cache_key} due to RPC error: {e}")
            return momentum_cache[cache_key][0]
        raise


async def process_single_momentum(client, filename):
    """Process momentum for a single token with safe file operations"""
    try:
        mint = filename.replace('.json', '')
        
        file_path = f'/home/shaolin_saga/data/pump_data/active_tokens/{filename}'
        data = safe_json_read(file_path, default=None, logger=momentum_logger)
        
        if data is None:
            momentum_logger.warning(f"Could not read active token data for {filename}")
            return
        
        curve_address = Pubkey.from_string(data['bondingCurve'])
        
        # Get cached bonding curve state
        bonding_curve_state = await get_cached_bonding_curve_state(client, curve_address)
        
        # Calculate current market cap
        token_price_sol = calculate_bonding_curve_price(bonding_curve_state)
        SOL_PRICE_USD = 136  # You might want to cache this.
        TOTAL_SUPPLY = 1000000000
        current_market_cap = SOL_PRICE_USD * token_price_sol * TOTAL_SUPPLY
        
        if not save_market_cap_state(mint, current_market_cap):
            momentum_logger.error(f"Failed to save market cap state for {mint}")
            return
        
        # Check momentum thresholds
        await check_momentum_thresholds(mint, current_market_cap)
        
        momentum_logger.debug(f"Processed momentum for {mint}: ${current_market_cap:,.2f}")
        
    except Exception as e:
        momentum_logger.error(f"Error processing momentum for {filename}: {e}")
        momentum_logger.error(f"Full traceback: {traceback.format_exc()}")


def save_market_cap_state(mint: str, market_cap: float, timestamp: float = None):
    """Save market cap state with optional timestamp using safe file operations"""
    if timestamp is None:
        timestamp = time.time()  # Use current time if not provided
    
    state_file = f'/home/shaolin_saga/data/pump_data/market_cap_states/{mint}.json'
    
    existing_states = safe_json_read(state_file, default=[], logger=momentum_logger)
    
    # Handle corrupted data gracefully
    if not isinstance(existing_states, list):
        momentum_logger.warning(f"Invalid state data format for {mint}, starting fresh")
        existing_states = []
    
    # Add new state
    existing_states.append({
        'market_cap': market_cap,
        'timestamp': timestamp
    })
    
    # Keep last 24 hours of data
    cutoff_time = time.time() - (24 * 3600)
    filtered_states = []
    
    for state in existing_states:
        # Validate state structure
        if isinstance(state, dict) and 'timestamp' in state and 'market_cap' in state:
            if state['timestamp'] > cutoff_time:
                filtered_states.append(state)
        else:
            momentum_logger.warning(f"Invalid state entry found for {mint}, skipping")
    
    if safe_json_write(state_file, filtered_states, logger=momentum_logger):
        momentum_logger.debug(f"Saved market cap state for {mint}: ${market_cap:,.2f} (kept {len(filtered_states)} states)")
        return True
    else:
        momentum_logger.error(f"Failed to save market cap state for {mint}")
        return False


async def process_momentum_batch(filenames, batch_size=15):
    """Process momentum for a batch of tokens"""
    momentum_logger.info(f"Processing momentum batch of {len(filenames)} tokens")
    
    # Process in smaller batches to avoid overwhelming RPC
    for i in range(0, len(filenames), batch_size):
        batch = filenames[i:i + batch_size]
        
        try:
            async with AsyncClient(RPC_MOMENTUM_ENDPOINT) as client:
                # Process batch concurrently
                tasks = [process_single_momentum(client, filename) for filename in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Log any errors
                for j, result in enumerate(results):
                    if isinstance(result, Exception):
                        momentum_logger.error(f"Batch error for {batch[j]}: {result}")
        
        except Exception as e:
            momentum_logger.error(f"Batch processing error: {e}")
        
        # Rate limiting between batches
        await asyncio.sleep(1)
        momentum_logger.debug(f"Completed batch {i//batch_size + 1}/{(len(filenames)//batch_size) + 1}")


# Momentum processing
async def process_momentum():
    """Process momentum for tokens that need updates (2-minute threshold)"""
    momentum_logger.info("Starting time-based momentum processing")
    
    directory = '/home/shaolin_saga/data/pump_data/active_tokens'
    if not os.path.exists(directory):
        momentum_logger.warning("Active tokens directory not found")
        return

    # Get tokens that need momentum updates
    tokens_to_process = []
    current_time = time.time()
    total_tokens = 0
    
    try:
        all_files = [f for f in os.listdir(directory) if f.endswith('.json')]
        total_tokens = len(all_files)
        
        for filename in all_files:
            mint = filename.replace('.json', '')
            last_momentum_file = f'/home/shaolin_saga/data/pump_data/market_cap_states/{mint}.json'
            
            should_process = True
            if safe_file_exists(last_momentum_file):
                # Use safe JSON read
                history = safe_json_read(last_momentum_file, default=[], logger=momentum_logger)
                
                if history and isinstance(history, list) and len(history) > 0:
                    last_update = history[-1].get('timestamp', 0)
                    time_since_update = current_time - last_update
                    
                    # Only process if last update was more than 2 minutes ago
                    if time_since_update < 120:  # 2 minutes
                        should_process = False
                        momentum_logger.debug(f"Skipping {mint} - last updated {time_since_update:.0f}s ago")
            
            if should_process:
                tokens_to_process.append(filename)
    
    except Exception as e:
        momentum_logger.error(f"Error filtering tokens for momentum processing: {e}")
        return
    
    momentum_logger.info(f"Processing momentum for {len(tokens_to_process)} tokens (out of {total_tokens} total)")
    momentum_logger.info(f"Skipped {total_tokens - len(tokens_to_process)} tokens (updated within 2 minutes)")
    
    if tokens_to_process:
        # Process in batches - can use larger batches since we have fewer tokens
        await process_momentum_batch(tokens_to_process, batch_size=25) #100 reduced to 25
    else:
        momentum_logger.info("No tokens need momentum updates at this time")


def momentum_monitor():
    momentum_logger.info("Starting momentum monitor")
    
    while True:
        momentum_logger.info("🌅 Starting next momentum cycle")
        
        try:
            # Start the processing but DON'T wait for it
            future = asyncio.run_coroutine_threadsafe(process_momentum(), bot.loop)
            
            # Now monitor until it's actually finished
            while processing_momentum:
                momentum_logger.info("⏳ Momentum processing ongoing, checking again in 60 seconds")
                time.sleep(60)
            
            momentum_logger.info("✅ Momentum processing finished, ready for next cycle")
            
        except Exception as e:
            momentum_logger.error(f"Error in momentum monitor: {e}")
            # Even if there's an error, wait for the flag to clear
            while processing_momentum:
                momentum_logger.info("⏳ Error occurred but processing still ongoing, waiting...")
                time.sleep(60)
        
        # Buffer before next cycle
        momentum_logger.info("😴 Brief pause before next cycle...")
        time.sleep(30)


async def check_momentum_thresholds(mint, current_market_cap):
    """Check if token meets momentum alert thresholds using safe file operations"""
    try:
        history_file = f'/home/shaolin_saga/data/pump_data/market_cap_states/{mint}.json'
        
        history = safe_json_read(history_file, default=[], logger=momentum_logger)
        
        if not history or len(history) < 2:
            momentum_logger.debug(f"Insufficient history for momentum check: {mint}")
            return False
        
        current_time = time.time()
        previous_data = None
        
        # Look for data point 1-3 minutes ago for 1-minute change
        for data_point in reversed(history[:-1]):
            time_diff = current_time - data_point['timestamp']
            if 60 <= time_diff <= 180:  # 1-3 minutes ago
                previous_data = data_point
                break
        
        if not previous_data:
            # Debug logging to see why no previous data found
            oldest_time_diff = current_time - history[0]['timestamp'] if history else 0
            newest_time_diff = current_time - history[-2]['timestamp'] if len(history) > 1 else 0
            momentum_logger.debug(f"No suitable previous data for {mint} - oldest: {oldest_time_diff:.0f}s, newest: {newest_time_diff:.0f}s")
            return False
        
        previous_market_cap = previous_data['market_cap']
        percentage_change = ((current_market_cap - previous_market_cap) / previous_market_cap) * 100
        
        # Check thresholds
        if abs(percentage_change) >= 10:  # 15% change threshold
            momentum_logger.info(f"🚀 Momentum alert triggered for {mint}: {percentage_change:+.2f}% change")
            await trigger_momentum_alert(mint, percentage_change, current_market_cap, previous_market_cap)
            return True
        
        return False
        
    except Exception as e:
        momentum_logger.error(f"Error checking momentum for {mint}: {e}")
        return False


async def trigger_momentum_alert(mint, percentage_change, current_market_cap, previous_market_cap):
    """Send momentum alert using your existing embed with market cap channel logic"""
    try:
        momentum_logger.info(f"Triggering momentum alert for {mint}: {percentage_change:+.2f}%")
        
        # Get token data
        tx_data = await get_saved_transaction_metadata(mint, momentum_logger)
        if not tx_data:
            momentum_logger.warning(f"No transaction data found for momentum alert: {mint}")
            return
        
        # Market cap channel selection logic
        current_mcap_float = float(current_market_cap)
        if current_mcap_float <= 10000:
            channel_type = 'momentum_sub_10k'
        elif 10000 < current_mcap_float <= 20000:
            channel_type = 'momentum_10k_20k'
        elif 20000 < current_mcap_float <= 30000:
            channel_type = 'momentum_20k_30k'
        else:
            channel_type = 'momentum_over_30k'
        
        # Prepare momentum data in the format your embed expects
        momentum_data = {
            '1m': percentage_change,  # 1-minute change percentage
            'market_cap': current_mcap_float,
            'channel': channel_type
        }
        
        # Create embed using your existing function
        embed = await create_momentum_embed(mint, momentum_data, tx_data, current_mcap_float)
        
        # Send to the appropriate momentum channel based on market cap
        for server in servers['allowed_servers']:
            channel = get_channel(server['server_id'], channel_type)
            if channel:
                await queue_discord_send(channel, embed, "momentum", momentum_logger, MessagePriority.HIGH)
                #await channel.send(embed=embed)
                momentum_logger.info(f"Sent momentum alert for {mint} (${current_mcap_float:,.2f}) to {channel_type} in server {server['server_id']}")
            else:
                momentum_logger.warning(f"Channel {channel_type} not found for server {server['server_id']}")
        
    except Exception as e:
        momentum_logger.error(f"Error sending momentum alert for {mint}: {e}")


#Setup  bonding curve
LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6

# Bonding discriminator
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)

class BondingCurveState:
    _STRUCT = Struct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag
    )

    def __init__(self, data: bytes) -> None:
        parsed = self._STRUCT.parse(data[8:])
        self.__dict__.update(parsed)


# Simplified get_bonding_curve_state function
async def get_bonding_curve_state(conn: AsyncClient, curve_address: Pubkey, max_retries: int = 3) -> BondingCurveState:
    """Get bonding curve state with built-in rate limiting"""
    response = await safe_rpc_call(
        conn.get_account_info, 
        curve_address,
        max_retries=max_retries,
        logger=rpc_logger
    )

    if not response.value or not response.value.data:
        bonding_logger.warning(f"No data in response for {curve_address}")
        raise ValueError("Invalid curve state: No data")

    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        bonding_logger.error(f"Invalid discriminator for {curve_address}{data}")
        raise ValueError("Invalid curve state discriminator")

    #bonding_logger.debug(f"✅ Successfully retrieved bonding curve state for {curve_address} {data}")
    return BondingCurveState(data)


#Calculate token price in SOL
def calculate_bonding_curve_price(curve_state: BondingCurveState) -> float:
    if curve_state.virtual_token_reserves <= 0 or curve_state.virtual_sol_reserves <= 0:
        raise ValueError("Invalid reserve state")

    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (curve_state.virtual_token_reserves / 10 ** TOKEN_DECIMALS)

async def create_momentum_embed(mint: str, momentum_data: dict, tx_data: dict, mcap: float):
    # Ensure all numeric values are floats
    mcap = float(mcap)
    momentum_data['1m'] = float(momentum_data['1m'])
    momentum_data['market_cap'] = float(momentum_data['market_cap'])


    name=tx_data['name']
    user=tx_data['user']
    symbol=tx_data['symbol']
    description=tx_data['description']
    twitter_url=tx_data['twitter_url']
    telegram_url=tx_data['telegram_url']
    website_url=tx_data['website_url']



    embed = discord.Embed(
        title=f"{name} ({symbol})",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )

    contract_uri = 'https://pump.fun/' + mint
    creator_uri = 'https://solscan.io/account/' + user

    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)
    embed.set_thumbnail(url=tx_data['image_url'])


    # Truncate desription
    if len(description) > 1000:  # Leave room for ellipsis
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)

    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

    # Add DexScreener indicator
    dex_status = green_tick if has_dexscreener_data(mint) else red_cross
    embed.add_field(name="Dex Paid?", value=dex_status, inline=True)
    
    embed.add_field(name="Market Cap", value=f'```${mcap:,.2f}```', inline=False)
    
    
    # Add 1-minute change percentage
    chart_up_emoji = "\U0001F4C8"
    embed.add_field(
        name="1 Minute Change",
        value=f"{chart_up_emoji} {momentum_data['1m']:.2f}%",
        inline=False
    ) 
    # Hotkeys section
    hotkeys = (
    f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
    return embed

def dexscreener_monitor():
    dexscreener_logger.info("Starting DexScreener monitor")
    while True:
        try:
            # Dex updates channel
            future1 = asyncio.run_coroutine_threadsafe(process_dexscreener_data(), bot.loop)
            dexscreener_logger.debug("process_dexscreener_data Started")
            try:
                future1.result(timeout=120)
            except asyncio.TimeoutError:
                dexscreener_logger.warning("process_dexscreener_data timed out")
            except Exception as e:
                dexscreener_logger.error("Error in process_dexscreener_data: %s", str(e))

            # Process latest boosted tokens
            future2 = asyncio.run_coroutine_threadsafe(process_boosted_tokens(), bot.loop)
            dexscreener_logger.debug("process_boosted_tokens Started")
            try:
                future2.result(timeout=120)
            except asyncio.TimeoutError:
                dexscreener_logger.warning("process_boosted_tokens timed out")
            except Exception as e:
                dexscreener_logger.error("Error in process_boosted_tokens: %s", str(e))

            # Process top boosted tokens
            future3 = asyncio.run_coroutine_threadsafe(process_top_boosts(), bot.loop)
            dexscreener_logger.debug("process_top_boosts Started")
            try:
                future3.result(timeout=120)
            except asyncio.TimeoutError:
                dexscreener_logger.warning("process_top_boosts timed out")
            except Exception as e:
                dexscreener_logger.error("Error in process_top_boosts: %s", str(e))

            dexscreener_logger.debug("DexScreener cycle complete, sleeping for 240 seconds")
            time.sleep(240)  # Check every 4 minutes

        except Exception as e:
            dexscreener_logger.error(f"Unexpected error in dexscreener_monitor: {str(e)}")
            dexscreener_logger.error(f"Full traceback: {traceback.format_exc()}")
            time.sleep(60)  # Sleep for a minute before retrying

async def fetch_dexscreener_top_boosts():
    """Fetch the top boosted tokens from DexScreener API"""
    url = "https://api.dexscreener.com/token-boosts/top/v1"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    dexscreener_logger.info(f"Successfully fetched top boosted tokens: {len(data)} tokens found")
                    return data
                else:
                    dexscreener_logger.error(f"Failed to fetch top boosted tokens: {response.status} - {await response.text()}")
                    return None
    except Exception as e:
        dexscreener_logger.error(f"Error fetching top boosted tokens: {str(e)}")
        return None


def extract_links(links_data):
    """Extract website, Twitter, and Telegram links from links array"""
    website_url = None
    twitter_url = None
    telegram_url = None
    
    if links_data and isinstance(links_data, list):
        for link in links_data:
            # Website link can be identified by 'label' being 'Website'
            if link.get('label') == 'Website':
                website_url = link.get('url')
            
            # Twitter link has type 'twitter'
            elif link.get('type') == 'twitter':
                twitter_url = link.get('url')
            
            # Telegram link has type 'telegram'
            elif link.get('type') == 'telegram':
                telegram_url = link.get('url')
    
    return website_url, twitter_url, telegram_url

async def process_top_boosts():
    """Process the top boosted tokens from DexScreener"""
    data = await fetch_dexscreener_top_boosts()
    dexscreener_logger.info(f"Dexscreener top boost found: {len(data)}")
    if not data:
        dexscreener_logger.error("Failed to fetch top boosted tokens")
        return

    # Filter for Solana tokens
    solana_tokens = [token for token in data if token.get('chainId') == 'solana']
    dexscreener_logger.info(f"Found {len(solana_tokens)} Solana top boosted tokens")

    for token in solana_tokens:
        # Extract links
        website_url, twitter_url, telegram_url = extract_links(token.get('links', []))
        
        token['extracted_website'] = website_url
        token['extracted_twitter'] = twitter_url
        token['extracted_telegram'] = telegram_url

    # Create directory if it doesn't exist
    os.makedirs('/home/shaolin_saga/data/dex_data/dexscreener_top_boosts', exist_ok=True)

    # Save the current top boosts for reference
    current_time = int(time.time())
    with open(f'/home/shaolin_saga/data/dex_data/dexscreener_top_boosts/top_boosts_{current_time}.json', 'w') as f:
        json.dump(solana_tokens, f)

    # Keep only the 10 most recent files
    top_boost_files = sorted(
        [f for f in os.listdir('/home/shaolin_saga/data/dex_data/dexscreener_top_boosts') if f.startswith('top_boosts_')],
        reverse=True
    )
    for old_file in top_boost_files[10:]:
        try:
            os.remove(os.path.join('/home/shaolin_saga/data/dex_data/dexscreener_top_boosts', old_file))
        except Exception as e:
            dexscreener_logger.error(f"Error removing old top boost file {old_file}: {str(e)}")

    # Process each token to check for new entries in top boosts
    processed_file = '/home/shaolin_saga/data/dex_data/dexscreener_top_boosts/processed_tokens.json'
    if os.path.exists(processed_file):
        with open(processed_file, 'r') as f:
            processed_tokens = json.load(f)
    else:
        processed_tokens = []

    new_top_tokens = []
    for token in solana_tokens:
        #dexscreener_logger.debug(f"TOP BOOST TOKENS: {token}")
        token_address = token.get('tokenAddress')
        if not token_address:
            continue

        # Check if this token is newly in the top boosts
        if token_address not in processed_tokens:
            processed_tokens.append(token_address)
            new_top_tokens.append(token)

    # Save the updated processed tokens list
    with open(processed_file, 'w') as f:
        json.dump(processed_tokens, f)

    # Send notifications for new top boosted tokens
    for token in new_top_tokens:
        await trigger_top_boost_notification(token)
        #dexscreener_logger.debug(f"TOP BOOS NOTIFICTAION SENT: {token}")

# emoji for embeds
lightning_emoji = "\u26A1"

# Embed for dexscreener_top_boosts_channel
async def trigger_top_boost_notification(token):
    #dexscreener_logger.debug(f"TOKEN FROM TOP BOOSTS: {token}")
    
    mint=token['tokenAddress']
    user = None
    token_name = None
    token_symbol = None
    twitter_url = None
    telegram_url = None
    website_url = None
    icon = token['icon']
   
    
    website_url = token.get('extracted_website')
    twitter_url = token.get('extracted_twitter')
    telegram_url = token.get('extracted_telegram')
    #dexscreener_logger.debug(f"DEX TOP BOOSTS  EMBED {website_url}{twitter_url}{telegram_url}")

    if "pump" in mint:
        tx_data = await get_saved_transaction_metadata(mint, dexscreener_logger)
        #dexscreener_logger.debug(f"PUMP DATA FROM TOP BOOSTS: {tx_data}")
    
        if tx_data:
            token_name = tx_data.get('name', 'Unkown')
            user = tx_data.get('user', 'Unknown')
            token_symbol = tx_data.get('symbol', 'Unkown')
            twitter_url = tx_data['twitter_url']
            telegram_url = tx_data['telegram_url']
            website_url = tx_data['website_url']
        else:
            dexscreener_logger.info(f"No pump metadata found for mint: {mint}")

    elif "bonk" in mint:  # Missing colon here in your original code
        tx_data = await get_saved_bonk_metadata(mint)
        #dexscreener_logger.debug(f"Bonk DATA FROM TOP BOOSTS: {tx_data}")
    
        if tx_data:
            token_name = tx_data.get('name', 'Unkown')
            user = tx_data.get('user', 'Unknown')
            token_symbol = tx_data.get('symbol', 'Unkown')
            twitter_url = tx_data['twitter_url']
            telegram_url = tx_data['telegram_url']
            website_url = tx_data['website_url']
        else:
            dexscreener_logger.info(f"No bonk metadata found for mint: {mint}")

    else:
        dexscreener_logger.info(f"No transaction data found: {mint}")

    embed = discord.Embed(
        title=f"{token_name} ({token_symbol})",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )

    
    #icon_url = "https://cdn.dexscreener.com/cms/images/{icon}"
    if icon:
        icon_url = f"https://cdn.dexscreener.com/cms/images/{icon}"
        dexscreener_logger.debug(f"ICON FROM TOP BOOSTS: {icon} {icon_url}")
        #embed.set_thumbnail(url=token['icon'])
        embed.set_thumbnail(url=icon_url)
    
    #if token.get('icon'):
    #   dexscreener_logger.debug(f"ICON FROM TOP BOOSTS: {icon}")
    #    embed.set_thumbnail(url=token['icon'])
    
    if token.get('header'):
        embed.set_image(url=token['header'])

    creator_uri = 'https://solscan.io/account/' + (user or 'Unknown')
    contract_uri = get_contract_uri_for_mint(mint)

    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)

    # Truncate desription
    description = token.get('description') or 'No Description Added'
    if len(description) > 1000:  # Leave room for ellipsis
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)

    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)


    amount = token.get('totalAmount')
    embed.add_field(name=f"Boosted Amount", value=f"{lightning_emoji} {amount}", inline=True)

    # Hotkeys section
    hotkeys = (
    f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

    # Send to all servers
    for server in servers['allowed_servers']:
        if 'dexscreener_top_boosts' in server['channels']:
            channel = get_channel(server['server_id'], 'dexscreener_top_boosts')
            if channel:
                await queue_discord_send(channel, embed, "dexscreener_top_boosts", dexscreener_logger, MessagePriority.MEDIUM)
                dexscreener_logger.info(f"Sent top boost notification for dexscreener_top_boosts to server {server['server_id']}")

    tg_text = format_dexscreener_boost(
        mint, token_name or 'Unknown', token_symbol or '?', user,
        token.get('description'), twitter_url, telegram_url, website_url,
        token.get('totalAmount'), "dexscreener_top_boosts"
    )
    for target in get_telegram_targets("dexscreener_top_boosts"):
        await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, "dexscreener_top_boosts", dexscreener_logger)


async def fetch_dexscreener_boosted_tokens():
    """Fetch the latest boosted tokens from DexScreener API"""
    url = "https://api.dexscreener.com/token-boosts/latest/v1"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    dexscreener_logger.info(f"Successfully fetched boosted tokens: {len(data)} tokens found")
                    return data
                else:
                    dexscreener_logger.error(f"Failed to fetch boosted tokens: {response.status}")
                    return None
    except Exception as e:
        dexscreener_logger.error(f"Error fetching boosted tokens: {str(e)}")
        return None

async def process_boosted_tokens():
    """Process the latest boosted tokens from DexScreener"""
    data = await fetch_dexscreener_boosted_tokens()  
    #dexscreener_logger.info(f"data from dex boosted: {data}")
    
    if not data:
        dexscreener_logger.error("Failed to fetch boosted tokens")
        return

    # Filter for Solana tokens
    solana_tokens = [token for token in data if token.get('chainId') == 'solana']
    dexscreener_logger.info(f"Found {len(solana_tokens)} Solana boosted tokens")
    
    for token in solana_tokens:
        # Extract links
        website_url, twitter_url, telegram_url = extract_links(token.get('links', []))

        token['extracted_website'] = website_url
        token['extracted_twitter'] = twitter_url
        token['extracted_telegram'] = telegram_url
    
    # Create directory if it doesn't exist
    os.makedirs('/home/shaolin_saga/data/dex_data/dexscreener_boosts', exist_ok=True)

    # Process each token
    for token in solana_tokens:
        token_address = token.get('tokenAddress')
        if not token_address:
            continue
        
        #dexscreener_logger.info(f"Dexscreener boosted: {token_address}")
        # Create a unique ID for this boost
        boost_start = token.get('boostStartTimestamp', 0)
        boost_id = f"{token_address}_{boost_start}"

        # Check if we've already processed this boost
        boost_file = f"/home/shaolin_saga/data/dex_data/dexscreener_boosts/{boost_id}.json"
        if os.path.exists(boost_file):
            continue

        # Save the boost data
        with open(boost_file, 'w') as f:
            json.dump(token, f)

        # Trigger notification
        await trigger_dexscreener_boosted_embed(token)
        dexscreener_logger.info(f"Sending Dexscreener boosted {token}")

async def process_dexscreener_data():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    headers = {'Content-Type': 'application/json'}
    
    # Create directory if it doesn't exist
    os.makedirs('/home/shaolin_saga/data/dex_data/dexscreener', exist_ok=True)
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            data = await response.json()
            
            # Filter for Solana tokens
            solana_tokens = [token for token in data if token.get('chainId') == 'solana']
            #dexscreener_logger.info(f"Dexscreener boosted{solana_tokens}")    
            # Process each Solana token
            for token in solana_tokens:
                mint = token.get('tokenAddress')
                if not mint:
                    continue
                
                filename = f"/home/shaolin_saga/data/dex_data/dexscreener/{mint}.json"
                
                # Check if we've already processed this token
                is_new_token = not os.path.exists(filename)
                
                # Save token data to file (always update the file)
                with open(filename, 'w') as f:
                    json.dump(token, f)
                
                # Only trigger alert for new tokens
                if is_new_token:
                    await trigger_dexscreener_alert(token)
                    dexscreener_logger.info(f"sending dexscreener update{token}")

async def trigger_dexscreener_alert(data):
    embed = await create_dexscreener_embed(data)
    for server in servers['allowed_servers']:
        if 'dexscreener_updates' in server['channels']:  # Only try servers with the channel configured
            channel = get_channel(server['server_id'], 'dexscreener_updates')
            if channel:
                await queue_discord_send(channel, embed, "dexscreener_updates", websocket_logger, MessagePriority.MEDIUM)
                await asyncio.sleep(1)

    mint = data['tokenAddress']
    tg_name, tg_symbol, tg_user = 'Unknown', '?', None
    tg_twitter, tg_telegram, tg_website = None, None, None
    if "pump" in mint:
        tx_data = await get_saved_transaction_metadata(mint, dexscreener_logger)
        if tx_data:
            tg_name = tx_data['name']
            tg_symbol = tx_data['symbol']
            tg_user = tx_data['user']
            tg_twitter = tx_data['twitter_url']
            tg_telegram = tx_data['telegram_url']
            tg_website = tx_data['website_url']
    elif "bonk" in mint:
        tx_data = await get_saved_bonk_metadata(mint)
        if tx_data:
            tg_name = tx_data['name']
            tg_symbol = tx_data['symbol']
            tg_user = tx_data['user']
            tg_twitter = tx_data['twitter_url']
            tg_telegram = tx_data['telegram_url']
            tg_website = tx_data['website_url']
    tg_text = format_dexscreener_paid(
        mint, tg_name, tg_symbol, tg_user,
        data.get('description'), tg_twitter, tg_telegram, tg_website, data.get('url')
    )
    for target in get_telegram_targets("dexscreener_updates"):
        await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, "dexscreener_updates", dexscreener_logger)

async def get_metadata(uri):
    """Fetch metadata from IPFS with fallback gateways"""
    try:
        # Try original URI first
        async with aiohttp.ClientSession() as session:
            async with session.get(uri, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    websocket_logger.info(f"Successfully fetched metadata from original URI: {uri}")
                    return await response.json()
                else:
                    websocket_logger.warning(f"Original URI failed: {response.status} for {uri}")
        
        # Fallback: try Pinata gateway if original failed
        if '/ipfs/' in uri:
            ipfs_hash = uri[uri.index('/ipfs/') + 6:]
            pinata_uri = f'https://pump.mypinata.cloud/ipfs/{ipfs_hash}'
            
            async with aiohttp.ClientSession() as session:
                async with session.get(pinata_uri, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        websocket_logger.info(f"Successfully fetched from Pinata: {pinata_uri}")
                        return await response.json()
                    else:
                        websocket_logger.error(f"Pinata fetch failed: {response.status} for {pinata_uri}")
        
        return {}
        
    except Exception as e:
        websocket_logger.error(f"Error fetching metadata for {uri}: {e}")
        return {}

'''
async def get_metadata(uri):
    """Fetch metadata from IPFS with error handling"""
    websocket_logger.info(f"METADATA URI {uri}")
    try:
        ipfs_hash = uri[uri.index('/ipfs/') + 6:]
        new_uri = f'https://pump.mypinata.cloud/ipfs/{ipfs_hash}'
        
        async with aiohttp.ClientSession() as session:
            async with session.get(new_uri, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    websocket_logger.error(f"IPFS fetch failed: {response.status} for {new_uri}")
                    return {}
    except json.JSONDecodeError as e:
        websocket_logger.error(f"JSON decode error for {uri}: {e}")
        return {}
    except Exception as e:
        websocket_logger.error(f"Error fetching metadata for {uri}: {e}")
        return {}

async def get_metadata(uri):
    # Extract the IPFS hash from the URI
    ipfs_hash = uri[uri.index('/ipfs/') + 6:]

    # Replace the 4everland gateway with the ipfs.io gateway
    new_uri = 'https://pump.mypinata.cloud/ipfs/' + ipfs_hash
    
    #async with aiohttp.ClientSession() as session:
    #    async with session.get(new_uri) as response:
    #        return await response.json()


    # Make a request to the new URI
    response = requests.get(new_uri)
    return response.json()
'''

async def get_image_data(image_uri):
    if image_uri and '/ipfs/' in image_uri:
        ipfs_hash = image_uri[image_uri.index('/ipfs/') + 6:]
        return 'https://pump.mypinata.cloud/ipfs/' + ipfs_hash
    return image_uri


#Set tick or cross for socials
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

# Create the Discord embed - socials
async def create_embed(token_name, token_symbol, mint, image_url, twitter_url, telegram_url, website_url, user, description, bonding_curve):
    #async with AsyncClient(RPC_ENDPOINT) as client:
    #    await asyncio.sleep(9)  # Wait for RPC indexing
    #    top_holders = await get_top_holders(client, Pubkey.from_string(mint), logger=websocket_logger)
    #    print(f'{top_holders} from get_top_holders')
    #    websocket_logger.info(f"Top Holders from pump tokens {top_holders}")

        contract_uri = 'https://pump.fun/' + mint
        creator_uri = 'https://solscan.io/account/' + user
        
        embed = discord.Embed(
            title=f"{token_name} ({token_symbol})",
            color=0xFFD700,
            timestamp=datetime.datetime.utcnow()
        )
        
        embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

        embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
        embed.add_field(name="", value=f'```{mint}```', inline=False)
        embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
        embed.add_field(name="", value=f'```{user}```', inline=False)

        # Truncate desription
        if len(description) > 1000:
            description = description[:997] + "..."
        embed.add_field(name="Description", value=f'```{description}```', inline=False)
        
        embed.set_thumbnail(url=image_url)
        # Add clickable links if they exist
        embed.add_field(name="Socials", value="", inline=False)
        embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
        embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
        embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)
        
        # Add DexScreener indicator
        dex_status = green_tick if has_dexscreener_data(mint) else red_cross
        embed.add_field(name="Dex Paid?", value=dex_status, inline=True)


        # Add top holders
        #if top_holders:
        #    embed.add_field(name="Top Holders", value=f'```{top_holders}```', inline=False)
        '''
        # Add user token history
        if user_tokens:
            token_history = f"Creator has launched {len(user_tokens)} tokens\n"
            token_history += "Recent tokens:\n"
            for token in user_tokens[:3]:  # Show last 3 tokens
                 token_history += f"• {token['address']} ({token['reserves']:,.0f} tokens)\n"
            embed.add_field(name="Creator History", value=token_history, inline=False)
        '''
        current_unix_time = int(time.time())
        embed.add_field(name="Created Time", value=f"<t:{current_unix_time}:R>", inline=True)

        # Hotkeys section
        hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
        )
        embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
        embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
        return embed

# Create the Discord embed - dexscreener_updates_channel
async def create_dexscreener_embed(token):
    
    mint=token['tokenAddress']
    user = None
    token_name = None
    token_symbol = None
    twitter_url = None
    telegram_url = None
    website_url = None
    icon = token['icon']


    if "pump" in mint:
        tx_data = await get_saved_transaction_metadata(token['tokenAddress'], dexscreener_logger)
    
        if tx_data:
            token_name = tx_data['name']
            user = tx_data['user']
            token_symbol = tx_data['symbol']
            twitter_url = tx_data['twitter_url']
            telegram_url = tx_data['telegram_url']
            website_url = tx_data['website_url']
        else:
            dexscreener_logger.info(f"No pump metadata found for mint: {mint}")

    elif "bonk" in mint:
        tx_data = await get_saved_bonk_metadata(token['tokenAddress'])
    
        if tx_data:
            token_name = tx_data['name']
            user = tx_data['user']
            token_symbol = tx_data['symbol']
            twitter_url = tx_data['twitter_url']
            telegram_url = tx_data['telegram_url']
            website_url = tx_data['website_url']
        else:
            dexscreener_logger.info(f"No bonk metadata found for mint: {mint}")

    else:
        dexscreener_logger.info(f"No transaction data found: {mint}")

    embed = discord.Embed(
        title=f"{token_name} ({token_symbol})",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )

    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
    
    contract_uri = get_contract_uri_for_mint(mint)

    creator_uri = 'https://solscan.io/account/' + user

    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)
   

    if icon:
        embed.set_thumbnail(url=icon)
    
    if token.get('header'):
        embed.set_image(url=token['header'])
    
    if token.get('description'):
        description = token['description']
        if len(description) > 1000:
           description = description[:997] + "..."
        embed.add_field(name="Description", value=f'```{description}```', inline=False)

    
    embed.add_field(name="DexScreener", value=f"[View Profile]({token['url']})", inline=False)
    
    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

    # Add DexScreener indicator
    dex_status = green_tick if has_dexscreener_data(mint) else red_cross
    embed.add_field(name="Dex Paid?", value=dex_status, inline=True)
    
    
    # Hotkeys section
    hotkeys = (
    f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
    return embed


# Create the Discord embed - dexscreener_boosts_channel
async def trigger_dexscreener_boosted_embed(token):
    
    mint = token['tokenAddress']
    user = None
    token_name = None
    token_symbol = None
    twitter_url = None
    telegram_url = None
    website_url = None
    icon = token['icon']

    website_url = token.get('extracted_website')
    twitter_url = token.get('extracted_twitter')
    telegram_url = token.get('extracted_telegram')
   

    if "pump" in mint:
        tx_data = await get_saved_transaction_metadata(token['tokenAddress'], dexscreener_logger)
    
        if tx_data:
            token_name = tx_data['name']
            user = tx_data['user']
            token_symbol = tx_data['symbol']
            twitter_url = tx_data['twitter_url']
            telegram_url = tx_data['telegram_url']
            website_url = tx_data['website_url']
        else:
            dexscreener_logger.info(f"No pump metadata found for mint: {mint}")

    elif "bonk" in mint:
        tx_data = await get_saved_bonk_metadata(token['tokenAddress'])
    
        if tx_data:
            token_name = tx_data['name']
            user = tx_data['user']
            token_symbol = tx_data['symbol']
            twitter_url = tx_data['twitter_url']
            telegram_url = tx_data['telegram_url']
            website_url = tx_data['website_url']
        else:
            dexscreener_logger.info(f"No bonk metadata found for mint: {mint}")

    else:
        dexscreener_logger.info(f"No transaction data found: {mint}")

    embed = discord.Embed(
        title=f"{token_name} ({token_symbol})",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )

    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

    if icon:
        icon_uri = f"https://cdn.dexscreener.com/cms/images/{icon}"
        embed.set_thumbnail(url=icon_uri)
    
    if token.get('header'):
        embed.set_image(url=token['header'])

    contract_uri = get_contract_uri_for_mint(mint)
    creator_uri = 'https://solscan.io/account/' + user

    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)

    # Truncate desription
    description = token.get('description') or 'No Description Added'
    if len(description) > 1000:
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)
    
    # Add clickable links if they exist
    embed.add_field(name="Socials", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

    amount = token.get('totalAmount')
    embed.add_field(name=f"Boosted Amount", value=f"{lightning_emoji} {amount}", inline=True)
    
    # Hotkeys section
    hotkeys = (
    f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
     
    # Send to all servers
    for server in servers['allowed_servers']:
        if 'dexscreener_boosts' in server['channels']:
            channel = get_channel(server['server_id'], 'dexscreener_boosts')
            if channel:
                await queue_discord_send(channel, embed, "dexscreener_boosts", websocket_logger, MessagePriority.MEDIUM)
                dexscreener_logger.info(f"Sent boost notification for dexscreener_boosts to server {server['server_id']}")

    tg_text = format_dexscreener_boost(
        mint, token_name or 'Unknown', token_symbol or '?', user,
        token.get('description'), twitter_url, telegram_url, website_url,
        token.get('totalAmount'), "dexscreener_boosts"
    )
    for target in get_telegram_targets("dexscreener_boosts"):
        await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, "dexscreener_boosts", dexscreener_logger)


def calculate_time_to_bonding(created_timestamp: float) -> str:
    """Calculate time elapsed from token creation to bonding milestone"""
    elapsed_seconds = time.time() - created_timestamp
    
    hours = int(elapsed_seconds // 3600)
    minutes = int((elapsed_seconds % 3600) // 60)
    seconds = int(elapsed_seconds % 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


# Create the Discord embed
async def create_bonding_embed(bonding_curve, mint, stage):
    async with AsyncClient(RPC_ENDPOINT) as client:
        top_holders = await get_top_holders(client, Pubkey.from_string(mint), logger=websocket_logger)

        contract_uri = 'https://pump.fun/' + mint
        
        # Set stage-specific text
        if stage == "15percent":
            stage_text = "15 Percent Reached!"
            pump_link = f"[ PUMP ](https://pump.fun/coin/{mint})"
        elif stage == "35percent":
            stage_text = "35 Percent Reached!"
            pump_link = f"[ PUMP ](https://pump.fun/coin/{mint})"
        elif stage == "80percent":
            stage_text = "80 Percent Reached!"
            pump_link = f"[ PUMP ](https://pump.fun/coin/{mint})"
        elif stage == "95percent":
            stage_text = "95 Percent Reached!"
            pump_link = f"[ PUMP ](https://pump.fun/coin/{mint})"
        elif stage == "complete":
            stage_text = "Trading Now On PumpSwap!"
            pump_link = f"[ PUMPSWAP ](https://swap.pump.fun/?{mint}=&input=So11111111111111111111111111111111111111112&output={mint})"
        else:
            stage_text = stage  # Fallback to original value if unknown
            pump_link = f"[ PUMP ](https://pump.fun/coin/{mint})"

        token_name = None
        user = None
        token_symbol = None
        description = None
        twitter_url = None
        telegram_url = None
        website_url = None
        image_url = None
       

        tx_data = await get_saved_transaction_metadata(mint, bonding_logger)
        if tx_data:
            token_name = tx_data.get('name','Unknown')
            user = tx_data.get('user')
            token_symbol = tx_data.get('symbol','Unknown')
            twitter_url = tx_data.get('twitter_url')
            telegram_url = tx_data.get('telegram_url')
            website_url = tx_data.get('website_url')
            description = tx_data.get('description') or 'No Description Added'
            image_url = tx_data.get('image_url')
        else:
            bonding_logger.info(f"No transaction data found for mint: {mint}")
        
        if tx_data and 'created' in tx_data:
            time_to_bonding = calculate_time_to_bonding(tx_data['created'])

        creator_uri = 'https://solscan.io/account/' + user

        embed = discord.Embed(
            title=f"{token_name} ({token_symbol})",
            color=0xFFD700,
            timestamp=datetime.datetime.utcnow()
        )
        
        embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

        embed.add_field(name="Bonding Curve Alert", value=f'```{stage_text}```', inline=False)
        embed.add_field(name="Time to Milestone", value=f"```{time_to_bonding}```", inline=True)
        embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
        embed.add_field(name="", value=f'```{mint}```', inline=False)
        embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
        embed.add_field(name="", value=f'```{user}```', inline=False)
        embed.set_thumbnail(url=image_url)


        # Truncate desription
        if len(description) > 1000:
            description = description[:997] + "..."
        embed.add_field(name="Description", value=f'```{description}```', inline=False)
    
        # Add clickable links if they exist
        embed.add_field(name="Socials", value="", inline=False)
        embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
        embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
        embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)

        # Add DexScreener indicator
        dex_status = green_tick if has_dexscreener_data(mint) else red_cross
        embed.add_field(name="Dex Paid?", value=dex_status, inline=True)
        
        # Add top holders
        if top_holders:
            embed.add_field(name="Top Holders", value=f'```{top_holders}```', inline=False)


        #tx_data = await get_saved_transaction_metadata(mint, bonding_logger)
        #bonding_logger.debug("fetching txdata for created time")
        #if tx_data and 'created' in tx_data:
        #    time_to_bonding = calculate_time_to_bonding(tx_data['created'])
        #bonding_logger.debug("fetched time to bonding {time_to_bonding}")
        #embed.add_field(name="Time to Bonding", value=f"```{time_to_bonding}```", inline=True)

        # Hotkeys section
        hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | {pump_link}"
        )
        embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
        embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

        return embed

async def handle_message(data):
    # 1. Save minimal active token data (for bonding curve monitoring)
    if not save_active_token(data):
        websocket_logger.error(f"Failed to save active token {data['mint']} in handle message")

    # Get user's token history
    #user_tokens = await get_user_created_tokens(data['user'])
    #print(f"{user_tokens}")
    #for user_token in user_tokens:
    #    print(f"Token address: {user_token['address']}")


    # 2. Extract metadata and enrich data
    meta_uri = data.get('uri')
    if meta_uri is not None:
        token_data = await get_metadata(meta_uri)

     # Extract social URLs
    twitter_url = token_data.get('twitter') if token_data.get('twitter') and 'x.com' in token_data['twitter'].lower() else None
    telegram_url = token_data.get('telegram') if token_data.get('telegram') and 't.me' in token_data['telegram'].lower() else None
    website_url = token_data.get('website')
    
    # Calculate three_socials flag
    three_socials = bool(twitter_url and telegram_url and website_url)

    enriched_data = {
        'mint': data['mint'],              
        'bondingCurve': data['bondingCurve'],
        'user': data['user'],
        'name': data.get('name', 'Unknown'),
        'symbol': data.get('symbol', 'Unknown'),
        'image_url': await get_image_data(token_data.get('image')),
        'twitter_url': twitter_url,
        'telegram_url': telegram_url,
        'website_url': website_url,
        'description': token_data.get('description') or 'No Description Added',
        'three_socials': three_socials,
        'created': time.time()
    }
    print(enriched_data)

        
    if not save_token_metadata(data['mint'], enriched_data):
        websocket_logger.error(f"Failed to save metadata for {data['mint']} in handle message")


    # 3. Create and send new token embed
    embed = await create_embed(
        token_name=data.get('name', 'Unknown'),
        token_symbol=data.get('symbol', 'Unknown'),
        mint=data['mint'],
        image_url=enriched_data['image_url'],
        twitter_url=enriched_data['twitter_url'],
        telegram_url=enriched_data['telegram_url'],
        website_url=enriched_data['website_url'],
        user=data['user'],
        description=enriched_data['description'],
        #user_tokens=user_tokens,
        bonding_curve=data['bondingCurve']
    )
    
    # 4. Send to allowed servers
    for server in servers['allowed_servers']:
        channel = get_channel(server['server_id'], 'all_tokens')
        if channel:
            await queue_discord_send(channel, embed, "all_tokens", websocket_logger, MessagePriority.HIGH)
            #websocket_logger.info("QUEUED ALL TOKEN EMBED")
            #await asyncio.sleep(1)  # Add 1 second delay between sends

    # Telegram: all_tokens
    tg_text = format_all_tokens(enriched_data)
    for target in get_telegram_targets('all_tokens'):
        await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, 'all_tokens', websocket_logger)

    # 5.
    if three_socials:
        for server in servers['allowed_servers']:
            #websocket_logger.debug(f"Sent  3 socials in handle message")
            channel = get_channel(server['server_id'], 'new_coin_with_socials')
            if channel:
                await queue_discord_send(channel, embed, "new_coin_with_socials", websocket_logger, MessagePriority.HIGH)

        # Telegram: new_coin_with_socials
        tg_text_socials = format_new_coin_with_socials(enriched_data)
        for target in get_telegram_targets('new_coin_with_socials'):
            await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text_socials, 'new_coin_with_socials', websocket_logger)

def decode_create_instruction(ix_data, ix_def, accounts):
    args = {}
    offset = 8  # Skip 8-byte discriminator

    for arg in ix_def['args']:
        if arg['type'] == 'string':
            length = struct.unpack_from('<I', ix_data, offset)[0]
            offset += 4
            value = ix_data[offset:offset+length].decode('utf-8')
            offset += length
        elif arg['type'] == 'publicKey':
            value = base64.b64encode(ix_data[offset:offset+32]).decode('utf-8')
            offset += 32
        else:
            raise ValueError(f"Unsupported type: {arg['type']}")
        
        args[arg['name']] = value

    # Add accounts
    args['mint'] = str(accounts[0])
    args['bondingCurve'] = str(accounts[2])
    args['associatedBondingCurve'] = str(accounts[3])
    #args['user'] = str(accounts[7])
    # User is always at index 5 for CreateV2 (both standard and Mayhem)
    if len(accounts) >= 6:
        args['user'] = str(accounts[5])
    else:
        args['user'] = str(accounts[7]) 


    return args

async def listen_and_decode_create():
    idl = load_idl('/home/shaolin_saga/app/idl/pump_fun_idl.json')
    create_discriminators = {
        8576854823835016728,
        12984312444788445398
    }
        #rpc_monitor = RPCMonitor()

    while True:  # Outer reconnection loop
        try:
            async with websockets.connect(
                WSS_ENDPOINT,
                ping_interval=15,
                ping_timeout=10,
                close_timeout=5
            ) as websocket:
                websocket_logger.info("WebSocket connected successfully")

                subscription_message = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "blockSubscribe",
                    "params": [
                        {"mentionsAccountOrProgram": str(PUMP_PROGRAM)},
                        {
                            "commitment": "confirmed",
                            "encoding": "base64",
                            "showRewards": False,
                            "transactionDetails": "full",
                            "maxSupportedTransactionVersion": 0
                        }
                    ]
                })
                
                #Add reconnection delay
                await asyncio.sleep(2)

                await websocket.send(subscription_message)
                websocket_logger.info(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM}")

                while True:  # Inner message processing loop
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=30)
                        #rpc_monitor.add_request()
                        #current_rate = rpc_monitor.get_current_rate()

                        #if current_rate > 0:  # Only log when we have activity
                        #    websocket_logger.debug(f"Current RPC request rate: {current_rate}/sec")

                        # Add validation before parsing
                        if not response or not response.strip():
                            websocket_logger.warning("Received empty response, skipping...")
                            continue

                        data = json.loads(response)
                        
                        if 'error' in data:
                            error_code = data['error'].get('code')
                            error_message = data['error'].get('message', '')
                            
                            if error_code == -32005 or 'RPS limit' in error_message:
                                # Extract wait time from the error
                                try_again_in = data['error'].get('data', {}).get('try_again_in', '100ms')
                                websocket_logger.error(f"🚫 Rate limited! Waiting {try_again_in} before retry")
                                
                                # Parse the wait time properly
                                import re
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
                                    
                                    websocket_logger.info(f"⏳ Sleeping for {wait_seconds:.3f} seconds")
                                    await asyncio.sleep(wait_seconds + 0.1)  # Add 100ms buffer
                                else:
                                    # Fallback if parsing fails
                                    websocket_logger.warning("Could not parse wait time, using default 200ms")
                                    await asyncio.sleep(0.2)
                                
                                # Force reconnection to retry subscription
                                websocket_logger.info("🔄 Retrying subscription after rate limit")
                                break
                            else:
                                websocket_logger.error(f"RPC Error: {error_message}")
                                continue

                        #Websocket detailed debugging here:
                        message_type = data.get('method', 'Unknown')
                        #websocket_logger.debug(f"Received message type: {message_type}")

                        if message_type == 'Unknown':
                        # Log the full unknown message to understand what it is:
                            websocket_logger.info(f"🔍 UNKNOWN MESSAGE CONTENT: {json.dumps(data, indent=2)}")


                        if 'method' in data and data['method'] == 'blockNotification':
                            #websocket_logger.info(f"Message method: {data['method']}")
                            if 'params' in data and 'result' in data['params']:
                                block_data = data['params']['result']
                                if 'value' in block_data and 'block' in block_data['value']:
                                    block = block_data['value']['block']
                                    if 'transactions' in block:
                                        for tx in block['transactions']:
                                            if isinstance(tx, dict) and 'transaction' in tx:
                                                tx_data_decoded = base64.b64decode(tx['transaction'][0])
                                                transaction = VersionedTransaction.from_bytes(tx_data_decoded)

                                                for ix in transaction.message.instructions:
                                                    if str(transaction.message.account_keys[ix.program_id_index]) == str(PUMP_PROGRAM):
                                                        ix_data = bytes(ix.data)
                                                        discriminator = struct.unpack('<Q', ix_data[:8])[0]
                                                        #websocket_logger.info(f"Pump discriminator: {discriminator}")
                                                        if discriminator in create_discriminators:
                                                            create_ix = next(instr for instr in idl['instructions'] if instr['name'] == 'create')

                                                            # Add validation here
                                                            if all(index < len(transaction.message.account_keys) for index in ix.accounts):
                                                                account_keys = [str(transaction.message.account_keys[index]) for index in ix.accounts]
                                                                decoded_args = decode_create_instruction(ix_data, create_ix, account_keys)
                                                                await handle_message(decoded_args)
                                                                #websocket_logger.info(f"PUMP TOKEN CREATED")
                                                                print(json.dumps(decoded_args, indent=2))
                                                                print("--------------------")

                        elif 'result' in data:
                            websocket_logger.info("Subscription confirmed")
                        else:
                            websocket_logger.warning(f"❓ Unhandled message type: {message_type}")
                            websocket_logger.debug(f"Full message: {data}")
                    
                    except asyncio.TimeoutError:
                        if not websocket.open:
                            websocket_logger.info("Connection lost during timeout, triggering reconnect")
                            break  # Break inner loop to reconnect

                        try:
                            pong = await websocket.ping()
                            await asyncio.wait_for(pong, timeout=5)
                            websocket_logger.debug("Ping successful")
                        except Exception as ping_error:
                            websocket_logger.warning(f"Ping failed: {ping_error}, triggering reconnect")
                            break  # Break inner loop to reconnect

                    except json.JSONDecodeError as json_error:
                        websocket_logger.error(f"JSON decode error: {json_error}")
                        continue  # Skip this message, continue processing

                    except Exception as msg_error:
                        websocket_logger.error(f"Message processing error: {msg_error}")

                        # Check for any connection-related errors that require reconnection
                        error_str = str(msg_error).lower()
                        connection_errors = [
                            "keepalive ping timeout",
                            "no close frame received",
                            "no close frame sent",
                            "connection closed",
                            "1011",
                            "1006",
                            "connection lost"
                        ]

                        if any(error in error_str for error in connection_errors):

                            websocket_logger.error("WebSocket connection unstable, forcing reconnection")
                            break  # Exit inner loop to trigger reconnection
                        continue  # Skip this message, continue processing

        except websockets.exceptions.ConnectionClosedError as conn_error:
            websocket_logger.error(f"WebSocket connection closed: {conn_error}")
        except Exception as e:
            websocket_logger.error(f"Unexpected connection error: {e}")

        # Always sleep before reconnecting (this was missing!)
        websocket_logger.info("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    #Get message queue 
    message_queue = get_message_queue()
    await ensure_queue_processing()
    await ensure_telegram_queue_processing()

    asyncio.create_task(monitor_memory_usage(websocket_logger))

    
    #Start background tasks
    bot.loop.create_task(listen_and_decode_create())
    bot.loop.create_task(message_queue.process_queue())
    bot.loop.create_task(monitor_rate_limits(websocket_logger))
    bot.loop.create_task(log_rpc_stats(bonding_logger))
    threading.Thread(target=dexscreener_monitor, daemon=True).start()
    threading.Thread(target=bonding_curve_monitor, daemon=True).start()
    #threading.Thread(target=momentum_monitor, daemon=True).start()
    threading.Thread(target=pump_livestream_monitor, daemon=True).start()
    bot.loop.create_task(bonk_main.start_monitoring(bot, servers))
    bot.loop.create_task(bonk_bonding_monitor.start_monitoring(bot, servers))

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        command_logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        command_logger.error(f"Failed to sync commands: {e}")


@bot.event
async def on_guild_join(guild):
    """Automatically leave unauthorized servers"""
    allowed_server_ids = [server['server_id'] for server in servers['allowed_servers']]

    if guild.id not in allowed_server_ids:
        websocket_logger.warning(f"Bot added to unauthorized server: {guild.name} ({guild.id})")

        # Optional: DM the server owner
        try:
            await guild.owner.send(
                "Sorry, this bot is private and only available to authorized servers. "
                "Please contact the bot owner for access."
            )
        except:
            pass

        # Leave the server
        await guild.leave()
        websocket_logger.info(f"Left unauthorized server: {guild.name} ({guild.id})")
    else:
        websocket_logger.info(f"Bot successfully added to authorized server: {guild.name} ({guild.id})")

# Also update your existing @bot.check to be more comprehensive
@bot.check
async def global_check(ctx):
    """Block all commands from unauthorized servers"""
    if not ctx.guild:  # DM commands
        return False

    allowed_server_ids = [server['server_id'] for server in servers['allowed_servers']]
    return ctx.guild.id in allowed_server_ids

if __name__ == "__main__":
    bot.run(os.getenv('DISCORD_TOKEN'))


