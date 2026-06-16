import aiohttp
import io
import matplotlib.pyplot as plt
import squarify
import pandas as pd
import os
import discord
import sys
import traceback
import json
import random
#import datetime
import matplotlib.patheffects as path_effects
from discord import app_commands
from datetime import datetime
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solana.rpc.types import TokenAccountOpts
from base64 import b64decode
import struct
import logging

logger = logging.getLogger('commands')

# Import from utils file
from utils import get_token_metadata_by_mint, get_moralis_usd_price_cached, shorten_number, get_moralis_token_prices, is_valid_solana_address, get_top_holders, get_token_metadata, get_token_price, get_token_symbol, get_image_data, get_metadata, get_token_transactions, analyze_for_bundling

# Import from pnl calculator
#from pnl_calculator import get_pair_address, get_price_at_blocktime, calculate_pnl, blocktime_to_iso

# Import from config file
sys.path.append('/home/shaolin_saga/config')
from config import SOL, RPC_ENDPOINT, PUMP_PROGRAM, SYSTEM_TOKEN_PROGRAM, SS_ICON_URL, MORALIS_API_KEY

# Load allowed servers
with open('/home/shaolin_saga/config/servers.json', 'r') as server_file:
    servers = json.load(server_file)

# Checks command is run in specifc channel
def is_commands_channel(interaction: discord.Interaction) -> bool:
    """Check if the interaction is in the designated commands channel for the server"""
    server_id = interaction.guild_id

    for server in servers['allowed_servers']:
        if server['server_id'] == server_id:
            if 'commands_channel' in server:
                commands_channel = server['commands_channel']
                return interaction.channel_id == commands_channel
            else:
                return True

    return False

def get_commands_channel_mention(guild_id: int) -> str:
    """Get a mention string for the commands channel in a server"""
    for server in servers['allowed_servers']:
        if server['server_id'] == guild_id:
            if 'commands_channel' in server:
                channel_id = server['commands_channel']
                return f"<#{channel_id}>"

    return "the designated commands channel"

async def command_check(interaction: discord.Interaction) -> bool:
    if not is_commands_channel(interaction):
        channel_mention = get_commands_channel_mention(interaction.guild_id)
        await interaction.response.send_message(
            f"❌ Commands can only be used in {channel_mention}.",
            ephemeral=True
        )
        logger.warning(f"User {interaction.user} tried to use a command in restricted channel {interaction.channel_id}")
        return False
    return True

# Add admin channel check function
def is_admin_channel(interaction: discord.Interaction) -> bool:
    """Check if the interaction is in the designated admin channel for the server"""
    server_id = interaction.guild_id
    
    for server in servers['allowed_servers']:
        if server['server_id'] == server_id:
            if 'admin_channel' in server:
                admin_channel = server['admin_channel']
                return interaction.channel_id == admin_channel
            else:
                # If no admin_channel is specified, deny access
                return False
    
    return False

def get_admin_channel_mention(guild_id: int) -> str:
    """Get a mention string for the admin channel in a server"""
    for server in servers['allowed_servers']:
        if server['server_id'] == guild_id:
            if 'admin_channel' in server:
                channel_id = server['admin_channel']
                return f"<#{channel_id}>"
    return "the designated admin channel"

async def admin_check(interaction: discord.Interaction) -> bool:
    """Check if user is in admin channel"""
    if not is_admin_channel(interaction):
        channel_mention = get_admin_channel_mention(interaction.guild_id)
        await interaction.response.send_message(
            f"❌ Admin commands can only be used in {channel_mention}.",
            ephemeral=True
        )
        logger.warning(f"User {interaction.user} tried to use admin command in non-admin channel {interaction.channel_id}")
        return False
    return True



async def get_token_account_signatures(session, token_account_pubkey, limit=500):
    """
    Get transaction signatures for a specific token account using getSignaturesForAddress
    
    Args:
        session: aiohttp ClientSession
        token_account_pubkey: Public key of the token account
        limit: Maximum number of signatures to retrieve
        
    Returns:
        List of signature objects or empty list on error
    """
    try:
        request_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                token_account_pubkey,
                {"limit": limit}
            ]
        }
        
        async with session.post(RPC_ENDPOINT, json=request_data) as response:
            if response.status != 200:
                print(f"Error fetching signatures for {token_account_pubkey}: HTTP {response.status}")
                return []
            
            result = await response.json()
            signatures = result.get("result", [])
            
            print(f"[get_token_account_signatures] Found {len(signatures)} signatures for {token_account_pubkey}")
            return signatures
            
    except Exception as e:
        print(f"Error in get_token_account_signatures: {str(e)}")
        return []


def aggregate_token_transactions(transactions):
    """
    Aggregate transactions by mint to calculate totals.
    
    Args:
        transactions: List of transaction dicts from get_token_account_transactions()
        
    Returns:
        Dict with mint as key, containing:
        {
            "mint": str,
            "total_bought": float (sum of all BUY amounts),
            "total_sold": float (sum of all SELL amounts),
            "net_position": float (bought - sold),
            "total_sol_spent": float (sum of SOL spent on all trades),
            "avg_buy_price": float (total_sol_spent / total_bought, if bought > 0),
            "transaction_count": int
        }
    """
    tokens_summary = {}
    
    for tx in transactions:
        mint = tx.get("mint")
        if not mint:
            continue
        
        # Initialize mint entry if not exists
        if mint not in tokens_summary:
            tokens_summary[mint] = {
                "mint": mint,
                "total_bought": 0.0,
                "total_sold": 0.0,
                "transaction_count": 0
            }
        
        token_amount_change = tx.get("token_amount_change", 0)
        
        # Aggregate based on transaction type
        if token_amount_change > 0:  # BUY
            tokens_summary[mint]["total_bought"] += token_amount_change
        elif token_amount_change < 0:  # SELL
            tokens_summary[mint]["total_sold"] += abs(token_amount_change)
        #Count processed transactions 
        tokens_summary[mint]["transaction_count"] += 1
    
    # Calculate derived metrics
    for mint, summary in tokens_summary.items():
        # Net position (positive = holding, negative = short/sold more than bought)
        summary["net_position"] = summary["total_bought"] - summary["total_sold"]
        summary["avg_buy_price"] = 0.0
    
    return tokens_summary

async def get_token_account_transactions(session, signatures, wallet_address, logger):
    """
    Fetch and parse transaction details for each signature.
    Extracts mint, token amount change, SOL spent, and trade type.
    
    Args:
        session: aiohttp ClientSession
        signatures: List of signature objects from getSignaturesForAddress
        wallet_address: The wallet address (signer) to track
        logger: Logger instance
        
    Returns:
        List of parsed transaction data with structure:
        {
            "signature": str,
            "slot": int,
            "blockTime": int,
            "fee": int (lamports),
            "status": "success" or "failed",
            "mint": str,
            "token_amount_change": float (positive = buy, negative = sell),
            "sol_spent": float,
            "transaction_type": "BUY" or "SELL" or "UNKNOWN"
        }
    """
    transactions = []
    
    try:
        for sig_info in signatures:
            signature = sig_info.get("signature")
            if not signature:
                continue
            
            tx_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0
                    }
                ]
            }
            
            async with session.post(RPC_ENDPOINT, json=tx_request) as tx_response:
                if tx_response.status != 200:
                    logger.debug(f"Error fetching transaction {signature}: HTTP {tx_response.status}")
                    continue
                
                tx_result = await tx_response.json()
                tx_data = tx_result.get("result")
                logger.info(f"Found TX_DATA {tx_data}")
                
                if not tx_data:
                    logger.debug(f"No transaction data for signature: {signature}")
                    continue
                
                try:
                    meta = tx_data.get("meta", {})
                    
                    # Skip failed transactions
                    if meta.get("err") is not None:
                        logger.debug(f"Skipping failed transaction: {signature}")
                        continue
                   

                    # Check for burn instructions in parsed transaction data
                    burn_found = False
                    tx_message = tx_data.get("transaction", {}).get("message", {})
                    logger.info(f"Found TX_MESSAGE {tx_message}")
                    instructions = tx_message.get("instructions", [])

                    for instruction in instructions:
                        if instruction.get("program") == "spl-token":
                            parsed = instruction.get("parsed", {})
                            if parsed.get("type") == "burn":
                                info = parsed.get("info", {})
                                if info.get("authority") == wallet_address:
                                    mint = info.get("mint")
                                    amount = float(info.get("amount", 0))
                                    
                                    # Assuming 9 decimals (adjust if needed)
                                    token_amount_change = -(amount / 1e9)
                                    
                                    tx_parsed = {
                                        "signature": signature,
                                        "slot": tx_data.get("slot"),
                                        "blockTime": tx_data.get("blockTime"),
                                        "fee": meta.get("fee", 0),
                                        "status": "success",
                                        "mint": mint,
                                        "token_amount_change": token_amount_change,
                                        "transaction_type": "SELL"
                                    }
                                    transactions.append(tx_parsed)
                                    burn_found = True

                    if burn_found:
                        continue

                    # Extract basic transaction info
                    slot = tx_data.get("slot")
                    block_time = tx_data.get("blockTime")
                    fee = meta.get("fee", 0)
                    
                    # Get token balances
                    pre_token_balances = meta.get("preTokenBalances", [])
                    post_token_balances = meta.get("postTokenBalances", [])
                    
                    # Find wallet's token balance entries (match by mint + owner)
                    # Group by mint to handle multiple token accounts
                    mints_in_tx = {}
                    
                    for post_balance in post_token_balances:
                        if post_balance.get("owner") == wallet_address:
                            mint = post_balance.get("mint")
                            if mint:
                                mints_in_tx[mint] = {
                                    "post": post_balance,
                                    "pre": None
                                }
                    
                    # Match pre-balances
                    for pre_balance in pre_token_balances:
                        if pre_balance.get("owner") == wallet_address:
                            mint = pre_balance.get("mint")
                            if mint and mint in mints_in_tx:
                                mints_in_tx[mint]["pre"] = pre_balance

                    # If no token balances found for wallet, skip
                    if not mints_in_tx:
                        logger.debug(f"No token balances found for wallet in {signature}")
                        continue
                    
                    # Process each mint in this transaction
                    for mint, balances in mints_in_tx.items():
                        pre_balance_entry = balances.get("pre")
                        post_balance_entry = balances.get("post")
                        
                        if not post_balance_entry:
                            continue
                        

                        # Extract token amounts
                        pre_amount = 0.0
                        if pre_balance_entry:
                            ui_amount = pre_balance_entry.get("uiTokenAmount", {}).get("uiAmount")
                            pre_amount = float(ui_amount) if ui_amount is not None else 0.0

                        post_ui_amount = post_balance_entry.get("uiTokenAmount", {}).get("uiAmount")
                        post_amount = float(post_ui_amount) if post_ui_amount is not None else 0.0
                        
                        # Calculate token amount change
                        token_amount_change = post_amount - pre_amount
                        
                        # Skip if no change
                        if token_amount_change == 0:
                            continue
                        
                        # Determine transaction type
                        if token_amount_change > 0:
                            transaction_type = "BUY"
                        else:
                            transaction_type = "SELL"
                        
                        tx_parsed = {
                            "signature": signature,
                            "slot": slot,
                            "blockTime": block_time,
                            "fee": fee,
                            "status": "success",
                            "mint": mint,
                            "token_amount_change": token_amount_change,
                            "transaction_type": transaction_type
                        }
                        transactions.append(tx_parsed)

                except Exception as parse_error:
                    #logger.error(f"Error parsing transaction {signature}...{tx_data}: {str(parse_error)}")
                    logger.error(f"Error parsing transaction {signature}: {str(parse_error)}")
                    continue
        
        return transactions
        
    except Exception as e:
        logger.error(f"Error in get_token_account_transactions: {str(e)}")
        return transactions


def register_commands(bot, logger):
    """Register all slash commands with the bot"""

        # Command channel check - apply to all commands
    async def command_check(interaction: discord.Interaction) -> bool:
        if not is_commands_channel(interaction):
            await interaction.response.send_message(
                "❌ Commands can only be used in the designated commands channel.",
                ephemeral=True
            )
            logger.warning(f"User {interaction.user} tried to use a command in restricted channel {interaction.channel_id}")
            return False
        return True

    @bot.tree.command(
        name="top-holders", 
        description="Show the top token holders for a pump.fun token"
    )
    @app_commands.describe(
        token_address="The Solana address of the token mint"
    )

    @app_commands.check(command_check)
    async def top_holders(interaction: discord.Interaction, token_address: str):
        """Command to show top holders for a pump.fun token"""
        # Log the command usage
        logger.info(f"Command /top-holders used by {interaction.user} ({interaction.user.id}) with token: {token_address}")
        
        # Validate the token address
        if not is_valid_solana_address(token_address):
            await interaction.response.send_message(
                "❌ Invalid token address. Please provide a valid Solana address.", 
                ephemeral=True
            )
            logger.warning(f"Invalid token address provided: {token_address}")
            return
        
        # Defer the response since fetching data might take time
        await interaction.response.defer(thinking=True)
        
        try:
            # Fetch top holders data
            async with AsyncClient(RPC_ENDPOINT) as client:
                logger.info(f"Fetching top holders for token: {token_address}")
                
                # Get token metadata
                token_pubkey = Pubkey.from_string(token_address)
                token_info = await get_token_metadata(client, token_pubkey, logger)
                
                # Look up associatedBondingCurve for pump tokens
                bonding_curve_account = None
                active_token_path = f"/home/shaolin_saga/data/pump_data/active_tokens/{token_address}.json"
                if os.path.exists(active_token_path):
                    try:
                        with open(active_token_path, 'r') as f:
                            active_data = json.load(f)
                        bonding_curve_account = active_data.get('associatedBondingCurve')
                    except Exception:
                        pass

                # Get top holders
                holders = await get_top_holders(client, token_pubkey, limit=10, logger=logger, bonding_curve_account=bonding_curve_account)
                
                # Create embed
                embed = discord.Embed(
                    title=f"Top Holders for {token_info['name']} ({token_info['symbol']})",
                    description=f"Token: [{token_address}](https://solscan.io/token/{token_address})",
                    color=0xFFD700,
                    timestamp=datetime.utcnow()
                )

                embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

                if token_info.get('image_url'):
                    embed.set_thumbnail(url=token_info['image_url'])

                embed.add_field(name="Top Holders", value=f'```{holders}```', inline=False)
                

                # Hotkeys section
                hotkeys = (    
                f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_address}) | [ AXIOM ](https://axiom.trade/t/{token_address}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{token_address}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={token_address}) | [ DEXSCEENER ](https://dexscreener.com/solana/{token_address}) | [ PUMP ](https://pump.fun/coin/{token_address})"
                )

                embed.add_field(name="Quick Buys", value=hotkeys, inline=False)

                # Add footer
                embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
                
                # Send the embed
                await interaction.followup.send(embed=embed)
                logger.info(f"Successfully sent top holders for {token_address}")
        
        except Exception as e:
            logger.error(f"Error in top-holders command: {str(e)}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(
                f"An error occurred while fetching holder data: {str(e)}"
            )
   

    @bot.tree.command(
        name="check-dex", 
        description="Check if a token has paid for DEX listing"
    )

    @app_commands.describe(
        token_address="The Solana address of the token mint"
    )

    @app_commands.check(command_check)
    async def check_dex(interaction: discord.Interaction, token_address: str):
        """Command to check if a token has paid for DEX listing"""
        # Log the command usage
        logger.info(f"Command /check-dex used by {interaction.user} ({interaction.user.id}) with token: {token_address}")
    
        # Validate the token address
        if not is_valid_solana_address(token_address):
            await interaction.response.send_message(
                "❌ Invalid token address. Please provide a valid Solana address.", 
                ephemeral=True
            )   
            logger.warning(f"Invalid token address provided: {token_address}")
            return
        
        # Defer the response since checking might take time
        await interaction.response.defer(thinking=True)
    
        try:
            # Check if the token exists in the dexscreener directory
            dex_file_path = f"/home/shaolin_saga/data/dex_data/dexscreener/{token_address}.json"
        
            if os.path.exists(dex_file_path):
                # Token has paid for DEX listing
                logger.info(f"Token {token_address} has paid for DEX listing")
            
                # Load the token data to get images
                with open(dex_file_path, 'r') as f:
                    dex_data = json.load(f)
            
            
                # Get token metadata for additional info
                async with AsyncClient(RPC_ENDPOINT) as client:
                    token_pubkey = Pubkey.from_string(token_address)
                    token_info = await get_token_metadata(client, token_pubkey, logger)
            

                # Create embed
                embed = discord.Embed(
                    title=f"DEX Status for {token_info['name']} ({token_info['symbol']})",
                    description="✅ This token has paid for DEX listing!",
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                    #timestamp=datetime.now(datetime.timezone.utc)
                )

                embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")

                # Add token info
                embed.add_field(
                    name="Token Address",
                    value=f"{token_address}](https://solscan.io/token/{token_address})",
                    inline=False
                )   
            
                # Add DEX link if available
                if 'url' in dex_data:
                    embed.add_field(
                        name="DEX Link",
                        value=f"[View on DEXScreener]({dex_data['url']})",
                        inline=False
                )   
            
                # Set thumbnail if we have an image
                if 'icon' in dex_data:
                    embed.set_thumbnail(url=dex_data['icon'])
                elif token_info.get('image_url'):
                    embed.set_thumbnail(url=token_info['image_url'])
            
                # Set banner if available
                if 'header' in dex_data:
                    embed.set_image(url=dex_data['header'])
            
                # Hotkeys section
                hotkeys = (
                f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_address}) | [ AXIOM ](https://axiom.trade/t/{token_address}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{token_address}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={token_address}) | [ DEXSCEENER ](https://dexscreener.com/solana/{token_address}) | [ PUMP ](https://pump.fun/coin/{token_address})"
                )

                embed.add_field(name="Quick Buys", value=hotkeys, inline=False)

                # Add footer
                embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
            
                # Send the embed
                await interaction.followup.send(embed=embed)
                logger.info(f"Successfully sent DEX status for {token_address}")
            
            else:
                # Token has not paid for DEX listing
                logger.info(f"Token {token_address} has not paid for DEX listing")
            
                # Get token metadata for the embed
                async with AsyncClient(RPC_ENDPOINT) as client:
                    token_pubkey = Pubkey.from_string(token_address)
                    token_info = await get_token_metadata(client, token_pubkey, logger)
            
                # Create embed
                embed = discord.Embed(
                    title=f"DEX Status for {token_info['name']} ({token_info['symbol']})",
                    description="❌ This token has not paid for DEX listing.",
                    color=discord.Color.red(),
                    timestamp=datetime.utcnow()
                    #timestamp=datetime.now(datetime.timezone.utc)
                )

                embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
            
                # Add token info
                embed.add_field(
                    name="Token Address",
                    value=f"[{token_address}](https://solscan.io/token/{token_address})",
                    inline=False
                )   
            
                # Set thumbnail if we have an image
                if token_info.get('image_url'):
                    embed.set_thumbnail(url=token_info['image_url'])
        
                # Hotkeys section
                hotkeys = (
                f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_address}) | [ AXIOM ](https://axiom.trade/t/{token_address}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{token_address}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={token_address}) | [ DEXSCEENER ](https://dexscreener.com/solana/{token_address}) | [ PUMP ](https://pump.fun/coin/{token_address})"
                )

                embed.add_field(name="Quick Buys", value=hotkeys, inline=False)


                # Add footer
                embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
            
                # Send the embed
                await interaction.followup.send(embed=embed)
                logger.info(f"Successfully sent DEX status for {token_address}")
    
        except Exception as e:
            logger.error(f"Error in check-dex command: {str(e)}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(
                f"An error occurred while checking DEX status: {str(e)}"
            )

        
    @bot.tree.command(
        name="heatmap",
        description="Show the current cryptocurrency market heatmap"
    )
    @app_commands.check(command_check)
    async def heatmap(interaction: discord.Interaction):
        """Command to display a cryptocurrency market heatmap"""
        # Log the command usage
        logger.info(f"Command /heatmap used by {interaction.user} ({interaction.user.id})")
        
        # Defer the response since generating the heatmap might take time
        await interaction.response.defer(thinking=True)
        
        try:
            # Fetch cryptocurrency data from CoinGecko API
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.coingecko.com/api/v3/coins/markets"
                    "?vs_currency=usd"
                    "&order=market_cap_desc"
                    "&per_page=40"  # Top 40 cryptocurrencies
                    "&page=1"
                    "&sparkline=false"
                    "&price_change_percentage=24h"
                ) as response:
                    if response.status != 200:
                        raise Exception(f"CoinGecko API returned status {response.status}")
                    
                    data = await response.json()
            
            # Prepare data for heatmap
            df = pd.DataFrame(data)
            
            # Extract relevant columns
            df = df[['name', 'symbol', 'market_cap', 'current_price', 'price_change_percentage_24h']]
            
            # Replace None values with 0
            df['price_change_percentage_24h'] = df['price_change_percentage_24h'].fillna(0)
            
            # Format price and percentage for labels
            df['price_formatted'] = df['current_price'].apply(lambda x: f"${x:,.2f}" if x >= 1 else f"${x:.4f}")
            df['pct_formatted'] = df['price_change_percentage_24h'].apply(lambda x: f"{x:+.2f}%" if x != 0 else "0.00%")
            
            # Create labels for the heatmap
            df['label'] = df['symbol'].str.upper() + '\n' + df['price_formatted'] + '\n' + df['pct_formatted']
            
            # Create colors based on price change
            colors = []
            for change in df['price_change_percentage_24h']:
                if change > 0:
                    # Green with intensity based on positive change
                    intensity = min(1, change/10)
                    colors.append((0, 0.5 + (intensity * 0.5), 0))
                else:
                    # Red with intensity based on negative change
                    intensity = min(1, abs(change)/10)
                    colors.append((0.5 + (intensity * 0.5), 0, 0))
            
            # Create the heatmap
            plt.figure(figsize=(12, 8))
            plt.style.use('dark_background')  # Use dark background for better visibility
            
            # Use squarify with minimal padding
            ax = squarify.plot(
                sizes=df['market_cap'], 
                label=df['label'], 
                color=colors, 
                alpha=0.8, 
                pad=0.3,  # Reduced padding to make small rectangles more visible
                text_kwargs={'color': 'white', 'fontsize': 8}  # Slightly smaller font size
            )
            
            # Add text outlines for better readability
            for text in ax.texts:
                text.set_path_effects([
                    path_effects.Stroke(linewidth=1.5, foreground='black'),
                    path_effects.Normal()
                ])
            
            plt.axis('off')
            plt.title('Cryptocurrency Market Heatmap (Market Cap & 24h Change)', fontsize=16)
            
            # Add timestamp
            plt.figtext(0.02, 0.02, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", 
                    fontsize=8, color='white')
            
            # Add source attribution
            plt.figtext(0.98, 0.02, "Data: CoinGecko", fontsize=8, color='white', ha='right')
            
            # Save the plot to a bytes buffer
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            buf.seek(0)
            
            # Create a file-like object from the image data
            image_file = discord.File(buf, filename="crypto_heatmap.png")
            
            # Create embed
            embed = discord.Embed(
                title="Cryptocurrency Market Heatmap",
                description="Market cap (size), price ($), and 24h change (%) for top 40 cryptocurrencies",
                color=0xFFD700,
                timestamp=datetime.utcnow()
            )

            embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
            
            # Set the image in the embed
            embed.set_image(url="attachment://crypto_heatmap.png")
            
            # Add source information
            embed.add_field(
                name="Source",
                value="Data from [CoinGecko](https://www.coingecko.com/)",
                inline=False
            )
            
            # Add color legend
            embed.add_field(
                name="Legend",
                value="🟩 Green: Price increase (24h)\n🟥 Red: Price decrease (24h)\n📊 Size: Market capitalization",
                inline=False
            )
            
            # Add interactive link
            embed.add_field(
                name="Interactive Version",
                value="[CoinMarketCap Heatmap](https://coinmarketcap.com/crypto-heatmap/)",
                inline=False
            )
            
            token_address = None
            # Hotkeys section
            hotkeys = (
            f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_address}) | [ AXIOM ](https://axiom.trade/t/{token_address}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{token_address}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={token_address}) | [ DEXSCEENER ](https://dexscreener.com/solana/{token_address}) | [ PUMP ](https://pump.fun/coin/{token_address})"
            )

            embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
            # Add footer
            embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

            # Send the embed with the attached image
            await interaction.followup.send(embed=embed, file=image_file)
            logger.info("Successfully sent crypto heatmap")
        
        except Exception as e:
            logger.error(f"Error in heatmap command: {str(e)}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(
                f"An error occurred while generating the crypto heatmap: {str(e)}"
            )

    @bot.tree.command(
        name="wallet-analyzer",
        description="Fast wallet analysis - SOL performance and token holdings"
    )
    @app_commands.describe(
        wallet_address="The Solana wallet address to analyze"
    )
    @app_commands.check(command_check)
    async def wallet_analyzer_v3(interaction: discord.Interaction, wallet_address: str):
        """Fast wallet analyzer - SOL performance tracking"""
        logger.info(f"Command /wallet-analyzer-v3 used by {interaction.user} ({interaction.user.id}) with wallet: {wallet_address}")
        
        if not is_valid_solana_address(wallet_address):
            await interaction.response.send_message(
                "❌ Invalid wallet address. Please provide a valid Solana address.",
                ephemeral=True
            )
            logger.warning(f"Invalid wallet address provided: {wallet_address}")
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Get current SOL balance
                sol_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [wallet_address]
                }
                
                async with session.post(RPC_ENDPOINT, json=sol_request) as response:
                    result = await response.json()
                    current_sol = result["result"]["value"] / 1_000_000_000
                
                # 2. Get wallet transaction history to find initial balance
                sig_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet_address, {"limit": 1000}]
                }
                
                async with session.post(RPC_ENDPOINT, json=sig_request) as response:
                    result = await response.json()
                    signatures = result.get("result", [])
                
                # Estimate initial SOL (first transaction)
                initial_sol = 0
                if signatures:
                    # Get the oldest transaction to estimate starting balance
                    oldest_sig = signatures[-1]["signature"]
                    
                    tx_request = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            oldest_sig,
                            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
                        ]
                    }
                    
                    async with session.post(RPC_ENDPOINT, json=tx_request) as tx_response:
                        tx_result = await tx_response.json()
                        tx_data = tx_result.get("result")
                        
                        if tx_data:
                            meta = tx_data.get("meta", {})
                            accounts = tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])
                            
                            # Find wallet's index
                            for i, account in enumerate(accounts):
                                account_key = account if isinstance(account, str) else account.get("pubkey")
                                if account_key == wallet_address:
                                    pre_balances = meta.get("preBalances", [])
                                    if i < len(pre_balances):
                                        initial_sol = pre_balances[i] / 1_000_000_000
                                    break
                
                # Calculate net SOL change
                net_sol = current_sol - initial_sol
                
                # 3. Get all token accounts
                token_accounts = []
                for program_id in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", 
                                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
                    request_data = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            wallet_address,
                            {"programId": program_id},
                            {"encoding": "jsonParsed"}
                        ]
                    }
                    
                    async with session.post(RPC_ENDPOINT, json=request_data) as response:
                        result = await response.json()
                        if "result" in result:
                            token_accounts.extend(result["result"]["value"])
                
                # 4. Extract non-zero holdings
                holdings = []
                for account in token_accounts:
                    info = account["account"]["data"]["parsed"]["info"]
                    amount = float(info["tokenAmount"]["uiAmount"] or 0)
                    
                    if amount > 0:
                        holdings.append({
                            "mint": info["mint"],
                            "amount": amount,
                            "decimals": info["tokenAmount"]["decimals"]
                        })
                
                logger.info(f"Found {len(holdings)} non-zero holdings from {len(token_accounts)} accounts")
                
                # Sort by amount (descending)
                holdings.sort(key=lambda x: x["amount"], reverse=True)
                
                # Calculate net sol usd value
                sol_usd_price_data = await get_moralis_usd_price_cached(SOL, MORALIS_API_KEY, logger=logger)
                sol_usd = sol_usd_price_data['usdPrice']
                wallet_net_usd = net_sol * sol_usd

                # 5. Build embeds
                embeds = []
                tokens_per_embed = 20
                max_embeds = 3
                
                for embed_num in range(max_embeds):
                    start_idx = embed_num * tokens_per_embed
                    end_idx = start_idx + tokens_per_embed
                    tokens_slice = holdings[start_idx:end_idx]
                    
                    if not tokens_slice and embed_num > 0:
                        break
                    
                    embed = discord.Embed(
                        title="💼 Wallet Overview" if embed_num == 0 else "💼 Wallet Overview (cont.)",
                        description=f"Analysis for [{wallet_address}](https://solscan.io/account/{wallet_address})" if embed_num == 0 else "",
                        color=0xFFD700,
                        timestamp=datetime.utcnow()
                    )
                    embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL)
                    
                    # SOL Performance (first embed only)
                    if embed_num == 0:
                        performance_emoji = "✅" if net_sol > 0 else "❌" if net_sol < 0 else "➖"
                        
                        embed.add_field(
                            name="📊 SOL Performance",
                            value=f"**Initial:** {initial_sol:.2f} SOL\n"
                                f"**Current:** {current_sol:.2f} SOL\n"
                                f"**Net Change:** {net_sol:+.2f} SOL {performance_emoji}\n"
                                f"**Net Change:** {wallet_net_usd:+.2f} USD {performance_emoji}\n"
                                f"**Total Tokens:** {len(holdings)}",
                            inline=False
                        )
                    
                    # Build token holdings table
                    if tokens_slice:
                        table = "```\n"
                        table += f"{'Mint Address':<44} {'Amount':^10}\n"
                        table += "-" * 56 + "\n"
                        
                        for token in tokens_slice:
                            metadata = await get_token_metadata_by_mint(token['mint'], logger)
                            if metadata:
                                mint_short = metadata['token_name']
                            else:
                                mint_short = f"{token['mint']}"
                            amount = shorten_number(token['amount'])
                            
                            table += f"{mint_short:<44} {amount:^10}\n"
                        
                        table += "```"
                        
                        if len(table) > 1000:
                            table = table[:950] + "\n...\n```"
                        
                        embed.add_field(name="🪙 Token Holdings", value=table, inline=False)
                    
                    # Overflow banner
                    if embed_num == max_embeds - 1 and len(holdings) > (max_embeds * tokens_per_embed):
                        remaining = len(holdings) - (max_embeds * tokens_per_embed)
                        embed.add_field(
                            name="⚠️ More Tokens",
                            value=f"+ {remaining} more tokens not shown",
                            inline=False
                        )
                    
                    embed.set_footer(text="Powered by Shaolin Saga! | SOL-based view", icon_url=SS_ICON_URL)
                    embeds.append(embed)
                
                # Send all embeds
                for embed in embeds:
                    await interaction.followup.send(embed=embed)
                
                logger.info(f"Successfully sent wallet overview ({len(embeds)} embeds) for {wallet_address}")
        
        except Exception as e:
            logger.error(f"Error in wallet-analyzer-v3 command: {str(e)}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(f"An error occurred while analyzing the wallet: {str(e)}")

    @bot.tree.command(
        name="findimage", 
        description="Find and reverse search an image for a Solana token"
    )
    @app_commands.describe(
        token_address="The Solana address of the token"
    )
    
    @app_commands.check(command_check)
    async def findimage(interaction: discord.Interaction, token_address: str):
        """Command to find and reverse search a token's image"""
        # Log the command usage
        logger.info(f"Command /findimage used by {interaction.user} ({interaction.user.id}) with token: {token_address}")
        
        # Validate the token address
        if not is_valid_solana_address(token_address):
            await interaction.response.send_message(
                "❌ Invalid token address. Please provide a valid Solana address.", 
                ephemeral=True
            )
            logger.warning(f"Invalid token address provided: {token_address}")
            return
        
        # Defer the response since fetching data might take time
        await interaction.response.defer(thinking=True)
        
        try:
            image_url = None
            metadata_uri = None

            # First, check if this is a pump.fun token we already know about
            pump_token_path = f"/home/shaolin_saga/data/pump_data/metadata/{token_address}.json"
            if os.path.exists(pump_token_path):
                with open(pump_token_path, 'r') as f:
                    tx_data = json.load(f)
                
                # Extract metadata URI
                metadata_uri = tx_data.get('uri')
                token_name = tx_data.get('name', 'Unknown Token')
                token_symbol = tx_data.get('symbol', 'UNKNOWN')
                
                # Try direct image_url first (stored in our metadata JSON)
                image_url = tx_data.get('image_url')

                # Fall back to fetching from metadata URI
                if not image_url and metadata_uri:
                    token_data = await get_metadata(metadata_uri)
                    image_url = token_data.get('image')
                    if image_url:
                        image_url = await get_image_data(image_url)
            else:
                # Not a pump.fun token, fetch metadata from RPC
                async with AsyncClient(RPC_ENDPOINT) as client:
                    # Get token metadata
                    token_pubkey = Pubkey.from_string(token_address)
                    token_info = await get_token_metadata(client, token_pubkey, logger)
                    
                    token_name = token_info.get('name', 'Unknown Token')
                    token_symbol = token_info.get('symbol', 'UNKNOWN')
                    image_url = token_info.get('image_url')
                    metadata_uri = token_info.get('uri')
            
            # If we couldn't get an image URL, inform the user
            if not image_url:
                await interaction.followup.send(
                    f"❌ Could not find an image for token: {token_address}"
                )
                logger.warning(f"No image found for token: {token_address}")
                return
            
            # Generate reverse image search links
            google_search = f"https://www.google.com/searchbyimage?image_url={image_url}"
            yandex_search = f"https://yandex.com/images/search?rpt=imageview&url={image_url}"
            tineye_search = f"https://www.tineye.com/search/?url={image_url}"
            
            # Create embed
            embed = discord.Embed(
                title=f"🔍 Image Search: {token_name} ({token_symbol})",
                description=f"Token: [{token_address[:8]}...{token_address[-4:]}](https://solscan.io/token/{token_address})",
                color=0xFFD700,
                timestamp=datetime.utcnow()
            )

            embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
            
            # Set the image
            embed.set_image(url=image_url)
            
            # Add metadata info
            if metadata_uri:
                embed.add_field(
                    name="📜 Metadata",
                    value=f"[View Metadata]({metadata_uri})",
                    inline=True
                )
            
            # Add image URL
            embed.add_field(
                name="🖼️ Image URL",
                value=f"[View Image]({image_url})",
                inline=True
            )
            
            # Add reverse image search links
            embed.add_field(
                name="🔎 Reverse Image Search",
                value=f"[Google]({google_search}) | [Yandex]({yandex_search}) | [TinEye]({tineye_search})",
                inline=False
            )
            
            # Hotkeys section
            hotkeys = (
            f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_address}) | [ AXIOM ](https://axiom.trade/t/{token_address}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{token_address}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={token_address}) | [ DEXSCEENER ](https://dexscreener.com/solana/{token_address}) | [ PUMP ](https://pump.fun/coin/{token_address})"
            )

            embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
            # Add footer
            embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

            # Send the embed
            await interaction.followup.send(embed=embed)
            logger.info(f"Successfully sent image search for {token_address}")
        
        except Exception as e:
            logger.error(f"Error in findimage command: {str(e)}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(
                f"An error occurred while searching for the token image: {str(e)}"
            )


    @bot.tree.command(
        name="bundle-check", 
        description="Check a token for potential bundling activity"
    )
    @app_commands.describe(
        token_address="The Solana address of the token to check"
    )
    
    @app_commands.check(command_check)
    async def bundle_check(interaction: discord.Interaction, token_address: str):
        """Command to analyze a token for bundling activity"""
        # Log the command usage
        logger.info(f"Command /bundle-check used by {interaction.user} ({interaction.user.id}) with token: {token_address}")
        
        # Validate the token address
        if not is_valid_solana_address(token_address):
            await interaction.response.send_message(
                "❌ Invalid token address. Please provide a valid Solana address.", 
                ephemeral=True
            )
            logger.warning(f"Invalid token address provided: {token_address}")
            return
        
        # Defer the response since fetching and analyzing data might take time
        await interaction.response.defer(thinking=True)
        
        try:
            # Get token metadata if available
            token_name = "Unknown Token"
            token_symbol = "UNKNOWN"

            metadata = await get_token_metadata_by_mint(token_address, logger)
            if metadata:
                token_name = metadata.get('token_name', 'Unknown Token')
                token_symbol = metadata.get('token_symbol', 'UNKNOWN')

            title = (
                f"🔍 Bundle Analysis At Launch: {token_name} ({token_symbol})"
                if token_name != "Unknown Token"
                else "🔍 Bundle Analysis At Launch"
            )

            embed = discord.Embed(
                title=title,
                description=f"Analyzing token [`{token_address[:8]}...{token_address[-4:]}`](https://solscan.io/token/{token_address}) for potential bundling activity...",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
            
            async with AsyncClient(RPC_ENDPOINT) as client:
                # Get token's transaction signatures
                signatures = await get_token_transactions(client, token_address)
                
                if not signatures:
                    embed.add_field(
                        name="❌ Error",
                        value="Could not retrieve transaction history for this token.",
                        inline=False
                    )
                    await interaction.followup.send(embed=embed)
                    return
                
                # Analyze the transactions for bundling patterns
                bundle_analysis = await analyze_for_bundling(client, signatures, token_address)
                
                # Update embed with analysis results
                embed.color = discord.Color.green() if bundle_analysis['bundle_score'] < 50 else discord.Color.red()
                
                # Add bundle score field
                score = bundle_analysis['bundle_score']
                score_emoji = "🟢" if score < 30 else "🟠" if score < 70 else "🔴"
                
                embed.add_field(
                    name="Bundle Score",
                    value=f"{score_emoji} {score}/100",
                    inline=False
                )
                
                # Add buy wave info
                clusters = bundle_analysis['transaction_clusters']
                early_tx_count = bundle_analysis['early_tx_count']
                if clusters:
                    cluster_text = ""
                    for i, cluster in enumerate(clusters[:3]):
                        cluster_text += f"**Wave {i+1}:** {cluster['count']} of {early_tx_count} buys in second {i+1}\n"

                    if len(clusters) > 3:
                        cluster_text += f"*+ {len(clusters) - 3} more waves...*\n"

                    embed.add_field(
                        name="Early Transactions",
                        value=cluster_text,
                        inline=False
                    )
                
                # Add wallet analysis
                wallet_analysis = bundle_analysis['wallet_analysis']
                embed.add_field(
                    name="Wallet Analysis",
                    value=f"**Unique Wallets:** {wallet_analysis['unique_wallets']}\n"
                        f"**Similar Buy Amounts:** {wallet_analysis['similar_buys']}\n"
                        f"**Early TX Bundled %:** {wallet_analysis['early_tx_bundled_percentage']:.1f}%",
                    inline=False
                )
                
                # Add conclusion
                risk_level = "Low" if score < 30 else "Medium" if score < 70 else "High"
                conclusion = f"**Risk Level:** {risk_level}\n\n"
                
                if score < 30:
                    conclusion += "✅ **Analysis:** No significant bundling activity detected. Early transactions appear organic."
                elif score < 70:
                    conclusion += "⚠️ **Analysis:** Some suspicious patterns detected. Moderate bundling activity possible."
                else:
                    conclusion += "❌ **Analysis:** Strong evidence of bundling activity. Multiple coordinated transactions detected."
                
                embed.add_field(
                    name="Conclusion",
                    value=conclusion,
                    inline=False
                )
                
               
                # Hotkeys section
                hotkeys = (
                f"[ PHOTON ](https://photon-sol.tinyastro.io/en/r/@codesaga/{token_address}) | [ AXIOM ](https://axiom.trade/t/{token_address}/@codesaga) | [ PADRE ](https://trade.padre.gg/trade/solana/{token_address}?rk=shaolinsaga) | [ BULLX ](https://neo.bullx.io/terminal?chainId=1399811149&address={token_address}) | [ DEXSCEENER ](https://dexscreener.com/solana/{token_address}) | [ PUMP ](https://pump.fun/coin/{token_address})"
                )   

                embed.add_field(name="Quick Buys", value=hotkeys, inline=False)
                # Add footer
                embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)

                # Send the embed
                await interaction.followup.send(embed=embed)
                logger.info(f"Successfully sent bundle analysis for {token_address}")
        
        except Exception as e:
            logger.error(f"Error in bundle-check command: {str(e)}")
            logger.error(traceback.format_exc())
            await interaction.followup.send(
                f"An error occurred while analyzing for bundling: {str(e)}"
            )


    @bot.tree.command(
        name="wallet-list",
        description="[ADMIN] List all tracked wallets in compact format"
    )
    @app_commands.describe(
        page="Page number to display (optional, default: 1)"
    )
    @app_commands.check(admin_check)
    async def wallet_list(interaction: discord.Interaction, page: int = 1):
        """List all tracked wallets in a compact text format with pagination"""
        logger.info(f"Command /wallet-list used by {interaction.user} ({interaction.user.id}) - Page {page}")
        
        await interaction.response.defer(thinking=True)
        
        try:
            #wallets_file = '/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets.json'
            
            server_id = interaction.guild_id
            wallets_file = f'/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets_{server_id}.json'


            # Create file if it doesn't exist
            if not os.path.exists(wallets_file):
                os.makedirs('/home/shaolin_saga/data/pump_data/tracked_wallets', exist_ok=True)
                with open(wallets_file, 'w') as f:
                    json.dump({"wallets": []}, f)
            
            # Load wallets
            with open(wallets_file, 'r') as f:
                data = json.load(f)
            
            wallets = data.get('wallets', [])
            
            if not wallets:
                await interaction.followup.send("```\nNo wallets are currently being tracked.\n```")
                return
            
            # Calculate pagination - each wallet line is ~120 chars, so ~15 wallets per page to stay under 2000 chars
            WALLETS_PER_PAGE = 15
            total_pages = (len(wallets) + WALLETS_PER_PAGE - 1) // WALLETS_PER_PAGE
            
            # Validate page number
            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages
            
            # Calculate start and end indices
            start_idx = (page - 1) * WALLETS_PER_PAGE
            end_idx = min(start_idx + WALLETS_PER_PAGE, len(wallets))
            page_wallets = wallets[start_idx:end_idx]
            
            # Create simple text table
            wallet_text = f"```\nTracked Wallets (Page {page}/{total_pages} - Showing {start_idx + 1}-{end_idx} of {len(wallets)}):\n\n"
            wallet_text += f"{'#':<4} {'Name':<20} {'Address':<45} {'Socials'}\n"
            wallet_text += "-" * 120 + "\n"
            
            for i, wallet in enumerate(page_wallets, start_idx + 1):
                name = wallet.get('name', 'Unknown')[:19]  # Truncate if too long
                address = wallet.get('wallet', 'Unknown')
                socials = wallet.get('socials', 'None')
                
                # Truncate socials if too long for display
                if len(socials) > 45:
                    socials = socials[:42] + "..."
                
                wallet_text += f"{i:<4} {name:<20} {address:<45} {socials}\n"
            
            # Add navigation info
            if total_pages > 1:
                wallet_text += "\n" + "-" * 120 + "\n"
                if page > 1:
                    wallet_text += f"Previous: /wallet-list page:{page-1}\n"
                if page < total_pages:
                    wallet_text += f"Next: /wallet-list page:{page+1}\n"
            
            wallet_text += "```"
            
            await interaction.followup.send(wallet_text)
            
        except Exception as e:
            logger.error(f"Error in wallet-list command: {str(e)}")
            await interaction.followup.send(f"An error occurred: {str(e)}")

    @bot.tree.command(
        name="wallet-add",
        description="[ADMIN] Add a wallet to the tracking list"
    )
    @app_commands.describe(
        wallet_address="The Solana wallet address to track",
        name="Display name for this wallet",
        socials="Social media links (optional)"
    )
    @app_commands.check(admin_check)
    async def wallet_add(interaction: discord.Interaction, wallet_address: str, name: str, socials: str = ""):
        """Add a wallet to tracking list"""
        logger.info(f"Command /wallet-add used by {interaction.user} ({interaction.user.id}) - Adding {name}: {wallet_address}")
        
        # Validate wallet address
        if not is_valid_solana_address(wallet_address):
            await interaction.response.send_message(
                "❌ Invalid wallet address. Please provide a valid Solana address.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            #wallets_file = '/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets.json'
            
            server_id = interaction.guild_id
            wallets_file = f'/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets_{server_id}.json'


            # Create file if it doesn't exist
            if not os.path.exists(wallets_file):
                os.makedirs('/home/shaolin_saga/data/pump_data/tracked_wallets', exist_ok=True)
                with open(wallets_file, 'w') as f:
                    json.dump({"wallets": []}, f)
            
            # Load existing wallets
            with open(wallets_file, 'r') as f:
                data = json.load(f)
            
            wallets = data.get('wallets', [])
            
            # Check if wallet already exists
            for wallet in wallets:
                if wallet.get('wallet') == wallet_address:
                    await interaction.followup.send(
                        f"❌ Wallet `{wallet_address}` is already being tracked as **{wallet.get('name', 'Unknown')}**"
                    )
                    return
            
            # Add new wallet
            new_wallet = {
                "name": name,
                "socials": socials if socials else "",
                "wallet": wallet_address
            }
            
            wallets.append(new_wallet)
            data['wallets'] = wallets
            
            # Save back to file
            with open(wallets_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Create success embed
            embed = discord.Embed(
                title="✅ Wallet Added Successfully",
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="Details",
                value=f"**Name:** {name}\n"
                    f"**Address:** [`{wallet_address}`](https://solscan.io/account/{wallet_address})\n"
                    f"**Socials:** {socials if socials else 'None'}",
                inline=False
            )
            
            embed.add_field(
                name="Total Tracked",
                value=f"Now tracking {len(wallets)} wallet(s)",
                inline=False
            )
            
            embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in wallet-add command: {str(e)}")
            await interaction.followup.send(f"An error occurred: {str(e)}")

    @bot.tree.command(
        name="wallet-remove",
        description="[ADMIN] Remove a wallet from the tracking list"
    )
    @app_commands.describe(
        identifier="Wallet address or name to remove"
    )
    @app_commands.check(admin_check)
    async def wallet_remove(interaction: discord.Interaction, identifier: str):
        """Remove a wallet from tracking list"""
        # Move defer() to the very beginning
        await interaction.response.defer(thinking=True)
        
        logger.info(f"Command /wallet-remove used by {interaction.user} ({interaction.user.id}) - Removing '{identifier}'")
        
        try:
            #wallets_file = '/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets.json'
            
            server_id = interaction.guild_id
            wallets_file = f'/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets_{server_id}.json'


            if not os.path.exists(wallets_file):
                await interaction.followup.send("❌ No wallets are currently being tracked.")
                return
            
            # Load existing wallets
            with open(wallets_file, 'r') as f:
                data = json.load(f)
            
            wallets = data.get('wallets', [])
            
            if not wallets:
                await interaction.followup.send("❌ No wallets are currently being tracked.")
                return
            
            # Find wallet to remove (by address or name)
            wallet_to_remove = None
            for wallet in wallets:
                if (wallet.get('wallet') == identifier or 
                    wallet.get('name', '').lower() == identifier.lower()):
                    wallet_to_remove = wallet
                    break
            
            if not wallet_to_remove:
                # Show available options
                available_names = [w.get('name', 'Unknown') for w in wallets[:5]]  # Show first 5
                
                await interaction.followup.send(
                    f"❌ No wallet found with address or name: `{identifier}`\n\n"
                    f"**Available names:** {', '.join(available_names)}\n"
                    f"Use `/wallet-list` to see all wallets."
                )
                return
            
            # Remove the wallet
            wallets.remove(wallet_to_remove)
            data['wallets'] = wallets
            
            # Save back to file
            with open(wallets_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Success message
            removed_name = wallet_to_remove.get('name', 'Unknown')
            removed_address = wallet_to_remove.get('wallet', 'Unknown')
            
            await interaction.followup.send(
                f"✅ **Wallet Removed Successfully**\n\n"
                f"**Name:** {removed_name}\n"
                f"**Address:** `{removed_address}`\n\n"
                f"Now tracking {len(wallets)} wallet(s)"
            )
            
        except Exception as e:
            logger.error(f"Error in wallet-remove command: {str(e)}")
            await interaction.followup.send(f"An error occurred: {str(e)}")

    @bot.tree.command(
        name="twitter-reuse",
        description="Check Twitter handle history for a user"
    )
    @app_commands.describe(
        identifier="Twitter handle (without @) or user ID"
    )
    @app_commands.check(command_check)
    async def twitter_history(interaction: discord.Interaction, identifier: str):
        """Check Twitter handle history using TweetScout API"""
        logger.info(f"Command /twitter-reuse used by {interaction.user} ({interaction.user.id}) for: {identifier}")
        
        await interaction.response.defer(thinking=True)
        
        try:
            # Clean the identifier (remove @ if present)
            clean_identifier = identifier.lstrip('@')

            # Determine lookup method
            how = "userid" if clean_identifier.isdigit() else "username"

            # Make API request
            headers = {
                'x-api-key': os.environ.get('TOTO_API_KEY', ''),
                'Content-Type': 'application/json'
            }
            payload = {"user": clean_identifier, "how": how, "page": 1}

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://toto.oz.xyz/api/metadata/get_past_usernames",
                    headers=headers,
                    json=payload
                ) as response:

                    if response.status == 404:
                        embed = discord.Embed(
                            title="❌ Account Not Found",
                            description=f"No Twitter handle history found for: `{identifier}`",
                            color=discord.Color.red(),
                            timestamp=datetime.utcnow()
                        )
                        embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
                        embed.add_field(
                            name="Note",
                            value="This account may no longer exist or no data is available.",
                            inline=False
                        )
                        embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
                        await interaction.followup.send(embed=embed)
                        return

                    elif response.status == 403:
                        await interaction.followup.send("❌ API access denied. Please check API key configuration.")
                        logger.error("Toto API returned 403 - check API key")
                        return

                    elif response.status != 200:
                        await interaction.followup.send(f"❌ API error: {response.status}")
                        logger.error(f"Toto API returned {response.status}: {await response.text()}")
                        return

                    data = await response.json()
                    # Map response to internal format: {handle, date}
                    raw = data.get('data', [])
                    handles = [{"handle": e.get("username", "Unknown"), "date": e.get("last_checked", "Unknown")} for e in raw]
                    
                    if not handles:
                        embed = discord.Embed(
                            title="📋 Twitter Handle History",
                            description=f"No handle changes found for: `{identifier}`",
                            color=0xFFD700,
                            timestamp=datetime.utcnow()
                        )
                        embed.set_author(name="Shaolin Saga", icon_url=SS_ICON_URL, url="")
                        embed.add_field(
                            name="Result",
                            value="This account has maintained the same handle since creation or first tracked.",
                            inline=False
                        )
                        embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
                        await interaction.followup.send(embed=embed)
                        return


                    # Sort handles by date (newest first)
                    def parse_date(date_str):
                        try:
                            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        except (ValueError, AttributeError):
                            return datetime.min  # fallback for invalid dates

                    handles.sort(key=lambda x: parse_date(x.get('date', '')), reverse=True)

                    
                    # Create embed
                    embed = discord.Embed(
                        title="📋 Twitter Handle History",
                        color=0xFFD700,
                        timestamp=datetime.utcnow()
                    )
                    
                    # Add current handle info
                    if handles:
                        current_handle = handles[0].get('handle', 'Unknown')
                        embed.add_field(
                            name=" ",
                            value=f"Current Handle: [@{current_handle}](https://twitter.com/{current_handle})",
                            inline=True
                        )
                    
                    # Create history table
                    if len(handles) > 1:
                        history_text = "```\n"
                        history_text += f"{'Date':<12} {'Handle':<20}\n"
                        history_text += "-" * 35 + "\n"
                        
                        for handle_data in handles:
                            date = handle_data.get('date', 'Unknown')
                            handle = handle_data.get('handle', 'Unknown')
                            
                            # Format date (assuming ISO format)
                            try:
                                if date != 'Unknown':
                                    date_obj = datetime.fromisoformat(date.replace('Z', '+00:00'))
                                    formatted_date = date_obj.strftime('%Y-%m-%d')
                                else:
                                    formatted_date = date
                            except:
                                formatted_date = date[:10] if len(date) >= 10 else date
                            
                            history_text += f"{formatted_date:<12} @{handle:<19}\n"
                        
                        history_text += "```"
                        
                        embed.add_field(
                            name=f"Handle History",
                            value=history_text,
                            inline=False
                        )
                    else:
                        embed.add_field(
                            name="History",
                            value="No handle changes detected - account has maintained the same handle.",
                            inline=False
                        )
                    
                    # Add summary
                    if len(handles) > 1:
                        oldest_handle = handles[-1].get('handle', 'Unknown')
                        embed.add_field(
                            name="Summary",
                            value=f"**Total Changes:** {len(handles) - 1}\n"
                                f"**Original Handle:** @{oldest_handle}\n"
                                f"**Current Handle:** @{current_handle}",
                            inline=False
                        )
                    
                    # Add warning if many changes
                    if len(handles) > 5:
                        embed.add_field(
                            name="⚠️ Notice",
                            value=f"This account has changed handles {len(handles) - 1} times, which may indicate suspicious activity.",
                            inline=False
                        )
                    
                    embed.set_footer(text="Powered by Shaolin Saga!", icon_url=SS_ICON_URL)
                    await interaction.followup.send(embed=embed)
                    
                    logger.info(f"Successfully retrieved handle history for {identifier}: {len(handles)} entries")
            
        except Exception as e:
            logger.error(f"Error in twitter-reuse command: {str(e)}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            await interaction.followup.send(f"An error occurred while fetching Twitter history: {str(e)}")


    return bot
'''
    @bot.tree.command(
        name="wallet-remove",
        description="[ADMIN] Remove a wallet from the tracking list"
    )
    @app_commands.describe(
        identifier="Wallet address or name to remove"
    )
    @app_commands.check(admin_check)
    async def wallet_remove(interaction: discord.Interaction, identifier: str):
        """Remove a wallet from tracking list"""
        logger.info(f"Command /wallet-remove used by {interaction.user} ({interaction.user.id}) - Removing {identifier}")
        
        await interaction.response.defer(thinking=True)
        
        try:
            wallets_file = '/home/shaolin_saga/data/pump_data/tracked_wallets/known_wallets.json'
            
            if not os.path.exists(wallets_file):
                await interaction.followup.send("❌ No wallets are currently being tracked.")
                return
            
            # Load existing wallets
            with open(wallets_file, 'r') as f:
                data = json.load(f)
            
            wallets = data.get('wallets', [])
            
            if not wallets:
                await interaction.followup.send("❌ No wallets are currently being tracked.")
                return
            
            # Find wallet to remove (by address or name)
            wallet_to_remove = None
            for wallet in wallets:
                if (wallet.get('wallet') == identifier or 
                    wallet.get('name', '').lower() == identifier.lower()):
                    wallet_to_remove = wallet
                    break
            
            if not wallet_to_remove:
                await interaction.followup.send(
                    f"❌ No wallet found with address or name: `{identifier}`\n"
                    f"Use `/wallet-list` to see all tracked wallets."
                )
                return
            
            # Remove the wallet
            wallets.remove(wallet_to_remove)
            data['wallets'] = wallets
            
            # Save back to file
            with open(wallets_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Create success embed
            embed = discord.Embed(
                title="✅ Wallet Removed Successfully",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            
            removed_name = wallet_to_remove.get('name', 'Unknown')
            removed_address = wallet_to_remove.get('wallet', 'Unknown')
            
            embed.add_field(
                name="Removed Wallet",
                value=f"**Name:** {removed_name}\n"
                    f"**Address:** `{removed_address}`",
                inline=False
            )
            
            embed.add_field(
                name="Total Tracked",
                value=f"Now tracking {len(wallets)} wallet(s)",
                inline=False
            )
            
            embed.set_footer(text="Powered by Shaolin Saga!")
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in wallet-remove command: {str(e)}")
            await interaction.followup.send(f"An error occurred: {str(e)}")
'''

    # Add more commands here as needed
    
    
    # Return the bot with commands registered
#    return bot

