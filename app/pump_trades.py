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
import sys
from dotenv import load_dotenv
import requests
import base64
import struct
import hashlib
import sys
import aiohttp
from utils import get_contract_uri_for_mint, get_saved_transaction_metadata, fetch_pump_fun_data, safe_json_read, safe_json_write, safe_file_move, safe_file_exists, safe_file_delete
from typing import Tuple
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey
from typing import Final, List, Tuple
from construct import Struct, Int64ul, Flag
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from collections import deque
from time import time as current_time
from rate_limiter import queue_discord_send, MessagePriority, get_message_queue, monitor_rate_limits, ensure_queue_processing, monitor_memory_usage
from telegram_sender import queue_telegram_send, get_telegram_targets, ensure_telegram_queue_processing
from telegram_formatter import format_whale_large_trade, format_wallet_tracker

#Get vars from config.py
sys.path.append('/home/shaolin_saga/config')
from config import WSS_ENDPOINT_SECONDARY, PUMP_PROGRAM, RPC_ENDPOINT_SECONDARY, SYSTEM_TOKEN_PROGRAM, SS_ICON_URL

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
buys_logger = setup_logger('buys', 'buys.log')
user_logger = setup_logger('user_monitor', 'user_monitor.log')
websocket_secondary_logger = setup_logger('websocket_secondary', 'websocket_secondary.log')
wallet_logger = setup_logger('wallet_monitor', 'wallet_monitor.log')

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

'''
async def check_tracked_wallet(user_address):
    """
    Check if a wallet address is in our tracked wallets list
    Returns the wallet data if found, None otherwise
    """
    try:
        # Create the directory if it doesn't exist
        os.makedirs('/home/shaolin_saga/data/tracked_wallets', exist_ok=True)

        # Path to the tracked wallets file
        #wallets_file = '/home/shaolin_saga/data/tracked_wallets/known_wallets.json'
        
        #server_id = interaction.guild_id
        wallets_file = f'/home/shaolin_saga/data/tracked_wallets/known_wallets_{server_id}.json'

        # Create the file with an empty structure if it doesn't exist
        if not os.path.exists(wallets_file):
            with open(wallets_file, 'w') as f:
                json.dump({"wallets": []}, f)
            return None

        # Load the tracked wallets
        with open(wallets_file, 'r') as f:
            tracked_wallets_data = json.load(f)

        # Check if the user address is in our tracked wallets
        for wallet in tracked_wallets_data.get('wallets', []):            
            #websocket_secondary_logger.debug(f"Wallet from checked tracked wallets: {wallet}")
            if wallet.get('wallet') == user_address:
                return {
                    'name': wallet.get('name', 'Unknown'),
                    'socials': wallet.get('socials', ''),
                    'wallet': wallet.get('wallet')
                }

        return None

    except Exception as e:
        websocket_secondary_logger.error(f"Error checking tracked wallet {user_address}: {str(e)}")
        return None
'''

# In-memory cache: {server_id: {'mtime': float, 'index': {address: wallet_info}}}
_wallet_cache = {}

def _load_wallet_cache(server_id, wallets_file):
    """Reload the wallet file into cache only if it has changed on disk."""
    try:
        mtime = os.path.getmtime(wallets_file)
    except OSError:
        return
    cached = _wallet_cache.get(server_id)
    if cached and cached['mtime'] == mtime:
        return  # Still fresh
    with open(wallets_file, 'r') as f:
        data = json.load(f)
    index = {
        w['wallet']: {
            'name': w.get('name', 'Unknown'),
            'socials': w.get('socials', ''),
            'wallet': w['wallet'],
            'server_id': server_id
        }
        for w in data.get('wallets', [])
        if w.get('wallet')
    }
    _wallet_cache[server_id] = {'mtime': mtime, 'index': index}
    wallet_logger.info(f"Reloaded wallet cache for server {server_id}: {len(index)} wallets")


async def check_tracked_wallet(user_address):
    """
    Check if a wallet address is in our tracked wallets list.
    Uses an mtime-based in-memory cache to avoid per-transaction file I/O.
    Returns the wallet data if found, None otherwise.
    """
    try:
        for server in servers['allowed_servers']:
            server_id = server['server_id']
            wallets_file = f'/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets_{server_id}.json'
            if not os.path.exists(wallets_file):
                continue
            _load_wallet_cache(server_id, wallets_file)
            wallet_info = _wallet_cache.get(server_id, {}).get('index', {}).get(user_address)
            if wallet_info:
                return wallet_info
        return None
    except Exception as e:
        wallet_logger.error(f"Error checking tracked wallet {user_address}: {str(e)}")
        return None


def user_monitor():
    user_logger.info("Starting user monitor")
    while True:
        future = asyncio.run_coroutine_threadsafe(process_all_users(), bot.loop)
        try:
            future.result(timeout=240)
        except asyncio.TimeoutError:
            user_logger.warning("process_all_users timed out")
        except Exception as e:
            user_logger.error(f"Error in process_all_users: {e}")
        user_logger.debug("User cycle complete, sleeping for 240 seconds")
        time.sleep(240)


async def process_all_users():
    user_logger.info("Starting to process users")
    users = {}

    try:
        # 1. Scan active tokens (recent creators)
        await scan_directory_for_creators('/home/shaolin_saga/data/active_tokens', users)

        # 2. Scan transaction archive (historical creators)
        #await scan_directory_for_creators('/home/shaolin_saga/data/transaction_archive', users)

        # 3. Check performance across bonding curve directories
        await analyze_creator_performance(users)

        # 4. Process based dev alerts
        await process_based_dev_alerts(users)

    except Exception as e:
        user_logger.error(f"Failed to process users: {str(e)}")

async def scan_directory_for_creators(directory, users):
    """Scan a directory for creator data"""
    if not os.path.exists(directory):
        user_logger.warning(f"Directory {directory} does not exist")
        return

    for filename in os.listdir(directory):
        if not filename.endswith('.json'):
            continue

        try:
            with open(f'{directory}/{filename}', 'r') as f:
                tx_data = json.load(f)

            creator = tx_data.get('user')
            mint = tx_data.get('mint', filename.replace('.json', ''))

            if creator and mint:
                if creator not in users:
                    users[creator] = {
                        'total_tokens': [],
                        'successful_tokens': [],
                        'performance_score': 0
                    }
                users[creator]['total_tokens'].append(mint)

        except Exception as e:
            user_logger.debug(f"Skipping file {filename}: {str(e)}")

async def analyze_creator_performance(users):
    """Check which tokens hit performance milestones"""
    performance_directories = [
        '/home/shaolin_saga/data/bondingComplete'
    ]

    for directory in performance_directories:
        if not os.path.exists(directory):
            continue

        for filename in os.listdir(directory):
            if not filename.endswith('.json'):
                continue

            try:
                with open(f'{directory}/{filename}', 'r') as f:
                    user_logger.debug(f"{directory}/{filename}")
                    performance_data = json.load(f)

                creator = performance_data.get('creator')
                mint = performance_data.get('mint', filename.replace('.json', ''))

                if creator and creator in users:
                    users[creator]['successful_tokens'].append({
                        'mint': mint,
                        'level': directory.split('/')[-1]
                    })
                    user_logger.info(f"{creator} from analyze creators")

                    # Score: 80% = 1 point, 95% = 3 points, complete = 5 points
                    if '80percent' in directory:
                        users[creator]['performance_score'] += 1
                    elif '95percent' in directory:
                        users[creator]['performance_score'] += 3
                    elif 'bondingComplete' in directory:
                        users[creator]['performance_score'] += 5

            except Exception as e:
                user_logger.debug(f"Error reading performance file {filename}: {str(e)}")

async def process_based_dev_alerts(users):
    """Process and send based dev alerts"""
    for user, data in users.items():
        total_tokens = len(data['total_tokens'])
        successful_tokens = len(data['successful_tokens'])
        performance_score = data['performance_score']

        # Criteria for "based dev"
        #should_alert = (
        #    total_tokens >= 3 and
        #    (successful_tokens >= 2 or performance_score >= 5)
        #)

        should_alert = (performance_score >= 5)

        if should_alert:
            # Check if already alerted
            user_file = f"/home/shaolin_saga/data/power_creators/{user}.json"
            already_alerted = False

            if os.path.exists(user_file):
                with open(user_file, 'r') as f:
                    existing_data = json.load(f)
                if (existing_data.get('performance_score', 0) >= performance_score and
                    existing_data.get('successful_tokens', 0) >= successful_tokens):
                    already_alerted = True

            if not already_alerted:
                # Save and alert
                save_enhanced_user_data(user, data)
                user_logger.info(f"BASED DEV: {user} - {successful_tokens}/{total_tokens} successful tokens (score: {performance_score})")

                embed = await create_power_creator_embed(user, data)
                if embed:
                    for server in servers['allowed_servers']:
                        channel = get_channel(server['server_id'], 'based_dev')
                        if channel:
                            await queue_discord_send(channel, embed, "based_dev", user_logger, MessagePriority.HIGH)


def save_enhanced_user_data(user, data):
    """Save enhanced power creator data with performance metrics"""
    os.makedirs('/home/shaolin_saga/data/power_creators', exist_ok=True)
    filename = f"/home/shaolin_saga/data/power_creators/{user}.json"

    save_data = {
        'user': user,
        'total_tokens': len(data['total_tokens']),
        'successful_tokens': len(data['successful_tokens']),
        'performance_score': data['performance_score'],
        'success_rate': len(data['successful_tokens']) / len(data['total_tokens']) * 100 if len(data['total_tokens']) > 0 else 0,
        'successful_token_details': data['successful_tokens'],
        'total_token_list': data['total_tokens'],
        'last_updated': datetime.datetime.utcnow().isoformat()
    }

    with open(filename, 'w') as f:
        json.dump(save_data, f, indent=2)


def save_user_data(user, count, mints=None):
    """Save power creator data to file"""
    os.makedirs('/home/shaolin_saga/data/power_creators', exist_ok=True)
    filename = f"/home/shaolin_saga/data/power_creators/{user}.json"

    data = {
        'user': user,
        'token_count': count,
        'last_updated': datetime.datetime.utcnow().isoformat()
    }

    if mints:
        data['mints'] = mints

    with open(filename, 'w') as f:
        json.dump(data, f)


fire_emoji = "\U0001F525"
chart_emoji = "\U0001F4C8"
target_emoji = "\U0001F3AF"


async def create_power_creator_embed(user, data):
    """Create Discord embed for a power creator with performance metrics"""
    # Get the most recent successful token, or fallback to any token
    if data['successful_tokens']:
        mint = data['successful_tokens'][-1]['mint']  # Most recent successful
    else:
        mint = data['total_tokens'][0]  # Fallback to first token
    
    contract_uri = 'https://pump.fun/' + mint
    creator_uri = 'https://solscan.io/account/' + user
    
    tx_data = await get_saved_transaction_metadata(mint, user_logger)
    if tx_data:
        token_name = tx_data.get('name','Unknown')
        user = tx_data.get('user')
        token_symbol = tx_data.get('symbol','Unknown')
        twitter_url = tx_data.get('twitter_url')
        telegram_url = tx_data.get('telegram_url')
        website_url = tx_data.get('website_url')
        description = tx_data.get('description') or 'No Description Available'
        image_url = tx_data.get('image_url')
    else:
        user_logger.info(f"No transaction data found for mint: {mint}")
        return None

    # Calculate success rate
    total_tokens = len(data['total_tokens'])
    successful_tokens = len(data['successful_tokens'])
    success_rate = (successful_tokens / total_tokens * 100) if total_tokens > 0 else 0

    """Create Discord embed for a power creator with multiple tokens"""
    embed = discord.Embed(
        title=f"{token_name} ({token_symbol})",
        description=f"{fire_emoji} BASED DEV: {successful_tokens}/{total_tokens} tokens hit 100% bonding curve!",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )
    
    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)
    #embed.add_field(name="Creator Address", value=f'```{user}```', inline=False)
    embed.set_thumbnail(url=image_url)
    
    # Truncate description
    if len(description) > 1000:  # Leave room for ellipsis
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)

    # Add clickable links if they exist
    embed.add_field(name="Social Media", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)
    
    # Show successful tokens instead of all tokens
    if data['successful_tokens']:
        successful_display = ""
        #level_emojis = {"80percent": "🟡 80%", "95percent": "🟠 95%", "bondingComplete": "🔴 Complete"}
        level_emojis = {
            "80percent": "\u2714 80%",
            "95percent": "\u2714 95%", 
            "bondingComplete": "\u2714 Complete"
        }



        for token in data['successful_tokens'][-9:]:  # Show last 5 successful
            level = token['level']
            emoji = level_emojis.get(level, "✅")
            successful_display += f"{token['mint']}... ({emoji})\n"
        
        if len(data['successful_tokens']) > 5:
            successful_display += f"+{len(data['successful_tokens'])-5} more successful tokens"
        
        #embed.add_field(name="🎯 Successful Tokens", value=f"```{successful_display}```", inline=False)
        embed.add_field(name=f"{target_emoji} Successful Tokens", value=f"```{successful_display}```", inline=False)

    
    # Add performance score
    #embed.add_field(
    #    name="📊 Performance Score", 
    #    value=f"```{data['performance_score']} points```", 
    #    inline=False
    #)

    embed.add_field(name=f"{chart_emoji} Success Rate!", value=f"```{success_rate:.1f}%```", inline=False)

    
    # Hotkeys section
    hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

    return embed


async def trigger_trade_alert(trade_data):

    mint = trade_data['mint']
    token_name = None
    token_symbol = None
    twitter_url = None
    telegram_url = None
    website_url = None
    description = None
    image_url = None


    tx_data = await get_saved_transaction_metadata(mint, buys_logger)
    if tx_data:
        token_name = tx_data.get('name','Unknown')
        user = tx_data.get('user')
        token_symbol = tx_data.get('symbol','Unknown')
        twitter_url = tx_data.get('twitter_url')
        telegram_url = tx_data.get('telegram_url')
        website_url = tx_data.get('website_url')
        description = tx_data.get('description')
        image_url = tx_data.get('image_url')
    else:
        buys_logger.info(f"No transaction data found for mint: {mint}")
    
    if token_name is None or token_name == 'Unknown':
        coin_data = await fetch_pump_fun_data(mint, buys_logger)
        if coin_data:
            token_name = coin_data.get('name','Unknown')
            user = coin_data.get('creator')
            token_symbol = coin_data.get('symbol','Unknown')
            twitter_url = coin_data.get('twitter')
            telegram_url = coin_data.get('telegram')
            website_url = coin_data.get('website')
            description = coin_data.get('description')
            image_url = coin_data.get('image_uri')
        else:
            buys_logger.info(f"No transaction data found on pump for mint: {mint}")

    # If token name is still none assume pre-pump
    #if token_name is None or token_name == 'Unknown':
    #    token_name = "Early Pump.Fun Transaction Detected – Token Not Yet Live"
    #    description = "Pre-launch activity detected – Pump.fun listing pending"

    contract_uri = 'https://pump.fun/' + mint
    # Determine alert type based on amount
    alert_type = "WHALE" if trade_data['sol_amount'] >= 10 else "LARGE"
        
    # Create embed with all details
    embed = discord.Embed(
        title=f"{token_name} ({token_symbol})",
        color=0xFFD700,
        timestamp=datetime.datetime.utcnow()
    )
   
    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

    user = trade_data['user']
    creator_uri = 'https://solscan.io/account/' + user

    embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
    embed.add_field(name="", value=f'```{mint}```', inline=False)
    embed.add_field(name="", value=f"[ Creator Address ]({creator_uri})", inline=False)
    embed.add_field(name="", value=f'```{user}```', inline=False)


    if image_url:
        embed.set_thumbnail(url=image_url)
    
    if description is None:
        description = 'No Description Added'

    if len(description) > 1000:  # Leave room for ellipsis
        description = description[:997] + "..."
    embed.add_field(name="Description", value=f'```{description}```', inline=False)


    embed.add_field(name="Transaction", value=f"[View on Solscan](https://solscan.io/tx/{trade_data['signature']})", inline=False)
    embed.add_field(name="Action", value=f"{'Purchased' if trade_data['type'] == 'buy' else 'Sold'} {trade_data['token_amount']:,.0f} {token_name}", inline=True)
    embed.add_field(name="Amount", value=f"{trade_data['sol_amount']:.2f} SOL", inline=True)
        
       
    # Add clickable links if they exist
    embed.add_field(name="Social Media", value="", inline=False)
    embed.add_field(name="", value=format_social_link(twitter_url, "Twitter"), inline=True)
    embed.add_field(name="", value=format_social_link(telegram_url, "Telegram"), inline=True)
    embed.add_field(name="", value=format_social_link(website_url, "Website"), inline=True)
    
    # Hotkeys section
    hotkeys = (
    f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
    )
    embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

    for server in servers['allowed_servers']:
        if token_name is None or token_name == 'Unknown':
            channel = get_channel(server['server_id'], 'pre_pump')
            channel_type = "pre_pump"
        elif trade_data['sol_amount'] >= 10:
            channel = get_channel(server['server_id'], 'whale_trades')
            channel_type = "whale_trades"
        else:
            channel = get_channel(server['server_id'], 'large_trades')
            channel_type = "large_trades"
                
        if channel:
            websocket_secondary_logger.info(f"{channel_type}")
            await queue_discord_send(channel, embed, channel_type, websocket_secondary_logger, MessagePriority.HIGH)
            await asyncio.sleep(0.5)  # Small delay between servers

    # Telegram: whale_trades and large_trades (not pre_pump)
    if channel_type in ("whale_trades", "large_trades"):
        tg_text = format_whale_large_trade(trade_data, token_name, token_symbol, mint, channel_type)
        for target in get_telegram_targets(channel_type):
            await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, channel_type, websocket_secondary_logger)


async def trigger_wallet_tracker_alert(trade_data):
    """
    Send an alert to the wallet tracker channel when a tracked wallet makes a trade
    """
    wallet_logger.info(f"wallet alert for {trade_data}")
    try:
        mint = trade_data.get('mint', 'Not Found')
        wallet_name = trade_data['wallet_name']
        wallet_address = trade_data['wallet_address']
        trade_type = trade_data['type']
        twitter_url = trade_data['socials']
        
        # Get token data
        tx_data = await get_saved_transaction_metadata(mint, wallet_logger)
        token_name = tx_data.get('name', 'Unknown') if tx_data else 'Unknown'
        token_symbol = tx_data.get('symbol', 'Unknown') if tx_data else 'Unknown'
        image_url = tx_data.get('image_url') if tx_data else None
        
        if trade_type == "BUY":
            embed_color = discord.Color.green()
            action_text = "🟢 **BUY**"
        elif trade_type == "SELL":
            embed_color = discord.Color.red()
            action_text = "🔴 **SELL**"
        else: 
            embed_color = 0xFFD700
        wallet_logger.info(f"TRADE TYPE {trade_type}")

        embed = discord.Embed(
            title=f"{token_name} ({token_symbol})",
            description=f"Wallet [{wallet_name}]({twitter_url}) has made a trade",
            color=embed_color,
            timestamp=datetime.datetime.utcnow()
        )

        embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
        
        contract_uri = get_contract_uri_for_mint(mint)
        wallet_uri = 'https://solscan.io/account/' + wallet_address
        
        embed.add_field(name="", value=f"[ Contract Address ]({contract_uri})", inline=False)
        embed.add_field(name="", value=f'```{mint}```', inline=False)
       
        embed.add_field(name="", value=f"[ Wallet Address ]({wallet_uri})", inline=False)
        embed.add_field(name="", value=f'```{wallet_address}```', inline=False)

        #embed.add_field(name="Action", value=action_text, inline=True)
        embed.add_field(name="Amount", value=f"{trade_data['sol_amount']:.2f} SOL", inline=True)
        
        embed.add_field(name="Action", value=action_text, inline=True)
        
        # Add transaction link
        embed.add_field(
            name="Transaction", 
            value=f"[View on Solscan](https://solscan.io/tx/{trade_data['signature']})",
            inline=False
        )
        
        if image_url:
            embed.set_thumbnail(url=image_url)
        
        hotkeys = (
        f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}) | [ AXIOM ](https://axiom.trade/t/{mint}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={mint}) | [ DEX ](https://dexscreener.com/solana/{mint}) | [ PUMP ](https://pump.fun/coin/{mint})"
        )
        embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
        embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

        # Send to all configured servers
        for server in servers['allowed_servers']:
            channel = get_channel(server['server_id'], 'wallet_tracker')
            if channel:
                await queue_discord_send(channel, embed, "wallet_tracker", wallet_logger, MessagePriority.LOW)
                wallet_logger.info(f"Sent wallet tracker alert for {wallet_address} to server {server['server_id']}")

        tg_text = format_wallet_tracker(
            mint, token_name, token_symbol,
            wallet_name, wallet_address, twitter_url,
            trade_type, trade_data['sol_amount'], trade_data['signature']
        )
        for target in get_telegram_targets("wallet_tracker"):
            await queue_telegram_send(target['chat_id'], target['thread_id'], tg_text, "wallet_tracker", wallet_logger)

    except Exception as e:
        wallet_logger.error(f"Error sending wallet tracker alert: {str(e)}")


def parse_token_transaction(tx_data, user_wallet):
    """
    Extract mint, SOL amount, and buy/sell type from getTransaction response
    """
    try:
        meta = tx_data["meta"]
        transaction = tx_data["transaction"]
        
        
        # 1. Get mint from postTokenBalances or preTokenBalances
        mint = None
        token_balances = meta.get("postTokenBalances", []) or meta.get("preTokenBalances", [])
        if token_balances:
            mint = token_balances[0].get("mint", "")

        # 2. Determine buy/sell from log messages
        trade_type = None
        for log in meta.get("logMessages", []):
            if "Instruction: Buy" in log:
                trade_type = "BUY"
                break
            elif "Instruction: Sell" in log:
                trade_type = "SELL"
                break
            else:
                trade_type = "Unknown"
        
        # 3. Calculate SOL amount using balance delta
        account_keys = [acc["pubkey"] if isinstance(acc, dict) else acc 
                       for acc in transaction["message"]["accountKeys"]]
        
        try:
            user_index = account_keys.index(user_wallet)
        except ValueError:
            return None, None, None
            
        pre_balance = meta["preBalances"][user_index]
        post_balance = meta["postBalances"][user_index]
        fee = meta["fee"]
        
        # Add back outgoing system transfers
        outgoing_transfers = 0
        for instruction in transaction["message"]["instructions"]:
            if (instruction.get("program") == "system" and 
                instruction.get("parsed", {}).get("type") == "transfer"):
                info = instruction["parsed"]["info"]
                if info.get("source") == user_wallet:
                    outgoing_transfers += int(info["lamports"])
        
        # Calculate net SOL from swap
        sol_amount_lamports = (post_balance - pre_balance) + fee + outgoing_transfers
        sol_amount = sol_amount_lamports / 1e9

        
        return mint, abs(sol_amount), trade_type
        
    except Exception as e:
        wallet_logger.error(f"Error parsing token transaction: {e}")
        return None, None, None


async def get_and_parse_transaction(signature, user_wallet):
    """
    Fetch transaction by signature and parse mint, SOL amount, and trade type
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "maxSupportedTransactionVersion": 0,
                "encoding": "jsonParsed"
            }
        ]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RPC_ENDPOINT_SECONDARY, json=payload) as response:
                if response.status != 200:
                    wallet_logger.error(f"HTTP error {response.status} fetching transaction {signature}")
                    return None, None, None
                
                result = await response.json()
                
                if not ("result" in result and result["result"]):
                    wallet_logger.error(f"No transaction found for signature: {signature}")
                    return None, None, None
                
                # Parse the transaction data
                tx_data = result["result"]
                return parse_token_transaction(tx_data, user_wallet)
                
    except Exception as e:
        wallet_logger.error(f"Error fetching/parsing transaction {signature}: {e}")
        return None, None, None


async def check_all_wallets_in_transaction(transaction, signature):
    """
    Check all wallet addresses in a transaction against tracked wallets list.
    This runs before PUMP program filtering to catch all wallet activity.
    """
    try:
        # Extract all unique wallet addresses from the transaction
        wallet_addresses = set()
        
        # Add all account keys (signers and non-signers)
        for account_key in transaction.message.account_keys:
            wallet_addresses.add(str(account_key))
        
        # Check each wallet address against tracked wallets
        for wallet_address in wallet_addresses:
            wallet_info = await check_tracked_wallet(wallet_address)
            if wallet_info:
                mint_address = None
                sig_data = await get_and_parse_transaction(signature, wallet_address)
                wallet_logger.info(f"DATA FROM SIG_DATA TRANSACTION: {sig_data}")
                mint_address, sol_amount, trade_type = sig_data

                # Create basic transaction data for the alert
                transaction_data = {
                    'wallet_address': wallet_address,
                    'signature': signature,
                    'wallet_name': wallet_info.get('name'),
                    'socials': wallet_info.get('socials'),
                    'type': trade_type,
                    'sol_amount': sol_amount,
                    'token_amount': 0,
                    'mint': mint_address
                }
                                 
                wallet_logger.info(f"Check all wallets tx data: {transaction_data}")
                # Trigger wallet tracker alert
                await trigger_wallet_tracker_alert(transaction_data)
                wallet_logger.info(f"Tracked wallet activity detected: {wallet_address}")
                
    except Exception as e:
        wallet_logger.error(f"Error checking wallets in transaction: {e}")

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
    args['user'] = str(accounts[7])

    return args

def decode_trade_instruction(ix_data, accounts, discriminator):
    BUY_DISCRIMINATOR = 16927863322537952870
    SELL_DISCRIMINATOR = 11235691341399839266

    """Decode buy/sell instruction data"""
    trade_data = {}

    # Common fields for both buy and sell
    trade_data['mint'] = str(accounts[2])  # Mint address
    trade_data['bonding_curve'] = str(accounts[3])  # Bonding curve
    trade_data['user'] = str(accounts[6])  # User address

    # Extract amounts based on instruction type
    if discriminator == BUY_DISCRIMINATOR:
        raw_token = struct.unpack("<Q", ix_data[8:16])[0]
        token_amount = raw_token / 1e6
        
        raw_sol = struct.unpack("<Q", ix_data[16:24])[0]
        
        # Add debug logging to see what we're working with
        #websocket_secondary_logger.debug(f"BUY - Raw SOL: {raw_sol}, Raw SOL length: {len(str(raw_sol))}")
        
        # Filter out problematic large values that cause false whale alerts
        # Lowered threshold to catch 11-digit values like 90000000000
        if raw_sol >= 1e11:  # 11+ digits (like 90000000000, 303000000000)
            sol_amount = 0  # Set to 0 to prevent false alerts
            websocket_secondary_logger.debug(f"Filtered out large raw SOL value: {raw_sol}")
        else:
            # Use consistent SOL scaling (standard lamports)
            sol_amount = raw_sol / 1e9

        trade_data['token_amount'] = token_amount
        trade_data['sol_amount'] = sol_amount
        trade_data['type'] = 'buy'
    else:  # SELL
        raw_token = struct.unpack("<Q", ix_data[8:16])[0]
        token_amount = raw_token / 1e6

        raw_sol = struct.unpack("<Q", ix_data[16:24])[0]
        
        # Add debug logging to see what we're working with
        websocket_secondary_logger.debug(f"SELL - Raw SOL: {raw_sol}, Raw SOL length: {len(str(raw_sol))}")
        
        # Filter out problematic large values that cause false whale alerts
        # Lowered threshold to catch 11-digit values like 90000000000
        if raw_sol >= 1e11:  # 11+ digits (like 90000000000, 303000000000)
            sol_amount = 0  # Set to 0 to prevent false alerts
            websocket_secondary_logger.debug(f"Filtered out large raw SOL value: {raw_sol}")
        else:
            # Use consistent SOL scaling (standard lamports)
            sol_amount = raw_sol / 1e9

        trade_data['token_amount'] = token_amount
        trade_data['sol_amount'] = sol_amount
        trade_data['type'] = 'sell'

    return trade_data

async def listen_and_decode_trades():
    BUY_DISCRIMINATOR = 16927863322537952870
    SELL_DISCRIMINATOR = 11235691341399839266
    #rpc_monitor = RPCMonitor()

    while True:  # Outer reconnection loop
        try:
            async with websockets.connect(
                WSS_ENDPOINT_SECONDARY,
                ping_interval=15,
                ping_timeout=10,
                close_timeout=5
            ) as websocket:
                websocket_secondary_logger.info("WebSocket connected successfully")

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
                websocket_secondary_logger.info(f"Subscribed to blocks mentioning program: {PUMP_PROGRAM}")

                while True:  # Inner message processing loop
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=20)
                        #rpc_monitor.add_request()
                        #current_rate = rpc_monitor.get_current_rate()
                        #websocket_secondary_logger.debug(f"Current RPC request rate: {current_rate}/sec")

                        # Add validation before parsing
                        if not response or not response.strip():
                            websocket_secondary_logger.warning("Received empty response, skipping...")
                            continue

                        data = json.loads(response)
                        
                        if 'error' in data:
                            error_code = data['error'].get('code')
                            error_message = data['error'].get('message', '')
                            
                            if error_code == -32005 or 'RPS limit' in error_message:
                                # Extract wait time from the error
                                try_again_in = data['error'].get('data', {}).get('try_again_in', '100ms')
                                websocket_secondary_logger.error(f"🚫 Rate limited! Waiting {try_again_in} before retry")
                                
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
                                    
                                    websocket_secondary_logger.info(f"⏳ Sleeping for {wait_seconds:.3f} seconds")
                                    await asyncio.sleep(wait_seconds + 0.1)  # Add 100ms buffer
                                else:
                                    # Fallback if parsing fails
                                    websocket_secondary_logger.warning("Could not parse wait time, using default 200ms")
                                    await asyncio.sleep(0.2)
                                
                                # Force reconnection to retry subscription
                                websocket_secondary_logger.info("🔄 Retrying subscription after rate limit")
                                break
                            else:
                                websocket_secondary_logger.error(f"RPC Error: {error_message}")
                                continue

                        #Websocket detailed debugging here:
                        message_type = data.get('method', 'Unknown')
                        websocket_secondary_logger.debug(f"Received message type: {message_type}")

                        if message_type == 'Unknown':
                        # Log the full unknown message to understand what it is:
                            websocket_secondary_logger.info(f"🔍 UNKNOWN MESSAGE CONTENT: {json.dumps(data, indent=2)}")


                        if 'method' in data and data['method'] == 'blockNotification':
                            if 'params' in data and 'result' in data['params']:
                                block_data = data['params']['result']
                                if 'value' in block_data and 'block' in block_data['value']:
                                    block = block_data['value']['block']
                                    if 'transactions' in block:
                                        for tx in block['transactions']:
                                            if isinstance(tx, dict) and 'transaction' in tx:
                                                tx_data_decoded = base64.b64decode(tx['transaction'][0])
                                                #buys_logger.debug(f"Trade Data: {tx_data_decoded}")
                                                
                                                transaction = VersionedTransaction.from_bytes(tx_data_decoded)
                                                signature = str(transaction.signatures[0]) if transaction.signatures else None

                                                await check_all_wallets_in_transaction(transaction, signature)
                                                #wallet_logger.info(f"CHECK ALL WALLETS: {transaction} {signature}")
                                                
                                                for ix in transaction.message.instructions:
                                                    if str(transaction.message.account_keys[ix.program_id_index]) == str(PUMP_PROGRAM):
                                                        ix_data = bytes(ix.data)
                                                        discriminator = struct.unpack('<Q', ix_data[:8])[0]

                                                        if discriminator in [BUY_DISCRIMINATOR, SELL_DISCRIMINATOR]:
                                                            if all(index < len(transaction.message.account_keys) for index in ix.accounts):
                                                                account_keys = [str(transaction.message.account_keys[index]) for index in ix.accounts]
                                                                trade_data = decode_trade_instruction(ix_data, account_keys, discriminator)
                                                                trade_data['signature'] = signature

                                                                print(json.dumps(trade_data, indent=2))
                                                                print("--------------------")

                                                                #websocket_secondary_logger.info(f"Trade detected: {trade_data['type']} {trade_data['token_amount']} tokens for {trade_data['sol_amount']} SOL")

                                                                if trade_data['sol_amount'] >= 3:
                                                                    await trigger_trade_alert(trade_data)

                    except asyncio.TimeoutError:
                        if not websocket.open:
                            websocket_secondary_logger.info("Connection lost, triggering reconnect")
                            break  # Break inner loop to reconnect

                        try:
                            pong = await websocket.ping()
                            await asyncio.wait_for(pong, timeout=5)
                            websocket_secondary_logger.debug("Ping successful")
                        except Exception as ping_error:
                            websocket_secondary_logger.info("Ping failed, triggering reconnect")
                            break  # Break inner loop to reconnect

                    except json.JSONDecodeError as json_error:
                        websocket_secondary_logger.error(f"JSON decode error: {json_error}")
                        continue  # Skip this message, continue processing
                    

                    except Exception as msg_error:
                        websocket_secondary_logger.error(f"Message processing error: {msg_error}")

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

                        #if "keepalive ping timeout" in str(msg_error) or "1011" in str(msg_error):
                            websocket_secondary_logger.error("WebSocket connection unstable, forcing reconnection")
                            break  # Exit inner loop to trigger reconnection
                        continue  # Skip this message, continue processing


        except websockets.exceptions.ConnectionClosedError as conn_error:
            websocket_secondary_logger.error(f"WebSocket connection closed: {conn_error}")
        except Exception as e:
            websocket_secondary_logger.error(f"Unexpected connection error: {e}")

        # Always sleep before reconnecting
        websocket_secondary_logger.info("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    #Get message queue
    message_queue = get_message_queue()
    await ensure_queue_processing()
    await ensure_telegram_queue_processing()

    asyncio.create_task(monitor_memory_usage(websocket_secondary_logger))
    bot.loop.create_task(listen_and_decode_trades())
    bot.loop.create_task(message_queue.process_queue())
    threading.Thread(target=user_monitor, daemon=True).start()


@bot.event
async def on_guild_join(guild):
    """Automatically leave unauthorized servers"""
    allowed_server_ids = [server['server_id'] for server in servers['allowed_servers']]

    if guild.id not in allowed_server_ids:
        user_logger.warning(f"Bot added to unauthorized server: {guild.name} ({guild.id})")

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
        user_logger.info(f"Left unauthorized server: {guild.name} ({guild.id})")
    else:
        user_logger.info(f"Bot successfully added to authorized server: {guild.name} ({guild.id})")

# Update your existing @bot.check
@bot.check
async def global_check(ctx):
    """Block all commands from unauthorized servers"""
    if not ctx.guild:  # DM commands
        return False

    allowed_server_ids = [server['server_id'] for server in servers['allowed_servers']]
    return ctx.guild.id in allowed_server_ids

'''
@bot.check
async def check_if_allowed_server(ctx):
    return any(server['server_id'] == ctx.guild.id for server in servers['allowed_servers'])
'''

if __name__ == "__main__":
    bot.run(os.getenv('DISCORD_TOKEN'))
