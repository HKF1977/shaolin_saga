"""
Telegram message formatters.
Each function takes a data dict and returns an HTML-formatted string
ready to send via the Telegram Bot API.
"""


def _socials_line(twitter_url, telegram_url, website_url) -> str:
    """Build a compact socials line from optional URLs."""
    parts = []
    if twitter_url:
        parts.append(f'<a href="{twitter_url}">Twitter</a>')
    if telegram_url:
        parts.append(f'<a href="{telegram_url}">Telegram</a>')
    if website_url:
        parts.append(f'<a href="{website_url}">Website</a>')
    return " | ".join(parts) if parts else "None"


def _trade_links(mint: str, platform: str = "pump") -> str:
    """Build quick-buy links line."""
    photon = f'<a href="https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}">Photon</a>'
    axiom = f'<a href="https://axiom.trade/t/{mint}/@codesaga">Axiom</a>'
    padre = f'<a href="https://trade.padre.gg/trade/solana/{mint}?rk=shaolinsaga">Padre</a>'
    dex = f'<a href="https://dexscreener.com/solana/{mint}">Dex</a>'

    if platform == "bonk":
        bonk = f'<a href="https://bonk.fun/token/{mint}">Bonk</a>'
        return f"{photon} | {axiom} | {padre} | {dex} | {bonk}"
    else:
        pump = f'<a href="https://pump.fun/{mint}">Pump</a>'
        return f"{photon} | {axiom} | {padre} | {dex} | {pump}"


def format_all_tokens(data: dict) -> str:
    """
    Formatter for pump.fun new token (all_tokens signal).
    Expects keys from enriched_data in handle_message().
    """
    mint = data.get('mint', '')
    name = data.get('name', 'Unknown')
    symbol = data.get('symbol', '?')
    description = data.get('description', 'No Description Added')
    creator = data.get('user', '')
    twitter_url = data.get('twitter_url', '')
    telegram_url = data.get('telegram_url', '')
    website_url = data.get('website_url', '')

    if description and len(description) > 200:
        description = description[:197] + "..."

    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint, platform="pump")
    creator_url = f"https://solscan.io/account/{creator}"

    return (
        f"🆕 <b>{name} (${symbol})</b>\n\n"
        f"<code>{mint}</code>\n\n"
        f"📝 {description}\n\n"
        f"👤 <a href=\"{creator_url}\">Creator</a>\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_new_coin_with_socials(data: dict) -> str:
    """
    Formatter for pump.fun new token with all 3 socials (new_coin_with_socials signal).
    Same data as format_all_tokens but highlighted differently.
    """
    mint = data.get('mint', '')
    name = data.get('name', 'Unknown')
    symbol = data.get('symbol', '?')
    description = data.get('description', 'No Description Added')
    creator = data.get('user', '')
    twitter_url = data.get('twitter_url', '')
    telegram_url = data.get('telegram_url', '')
    website_url = data.get('website_url', '')

    if description and len(description) > 200:
        description = description[:197] + "..."

    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint, platform="pump")
    creator_url = f"https://solscan.io/account/{creator}"

    return (
        f"🌐 <b>New Token — All 3 Socials!</b>\n\n"
        f"<b>{name} (${symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"📝 {description}\n\n"
        f"👤 <a href=\"{creator_url}\">Creator</a>\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_new_bonk_tokens(data: dict) -> str:
    """
    Formatter for bonk.fun new token (new_bonk_tokens signal).
    Expects keys from token_data in handle_bonk_token_creation().
    """
    mint = data.get('mint', '')
    name = data.get('name', 'Unknown')
    symbol = data.get('symbol', '?')
    description = data.get('description', 'No Description Added')
    creator = data.get('creator', '')
    twitter_url = data.get('twitter_url', '')
    telegram_url = data.get('telegram_url', '')
    website_url = data.get('website_url', '')

    if description and len(description) > 200:
        description = description[:197] + "..."

    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint, platform="bonk")
    creator_url = f"https://solscan.io/account/{creator}"

    return (
        f"🐕 <b>New Bonk Token!</b>\n\n"
        f"<b>{name} (${symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"📝 {description}\n\n"
        f"👤 <a href=\"{creator_url}\">Creator</a>\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_bonding_curve(mint: str, stage: str, metadata: dict) -> str:
    """
    Formatter for pump.fun bonding curve milestones (80% and complete).
    metadata comes from get_saved_transaction_metadata().
    """
    name = metadata.get('name', 'Unknown') if metadata else 'Unknown'
    symbol = metadata.get('symbol', '?') if metadata else '?'
    twitter_url = metadata.get('twitter_url', '') if metadata else ''
    telegram_url = metadata.get('telegram_url', '') if metadata else ''
    website_url = metadata.get('website_url', '') if metadata else ''

    if stage == "80percent":
        header = "🔥 <b>80% Bonding Curve Reached!</b>"
    elif stage == "complete":
        header = "💎 <b>100% Bonded — Now on PumpSwap!</b>"
        pump_link = f'<a href="https://swap.pump.fun/?{mint}=&input=So11111111111111111111111111111111111111112&output={mint}">PumpSwap</a>'
    else:
        header = f"📈 <b>Bonding Curve — {stage}</b>"

    socials = _socials_line(twitter_url, telegram_url, website_url)

    if stage == "complete":
        photon = f'<a href="https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}">Photon</a>'
        axiom = f'<a href="https://axiom.trade/t/{mint}/@codesaga">Axiom</a>'
        dex = f'<a href="https://dexscreener.com/solana/{mint}">Dex</a>'
        links = f"{photon} | {axiom} | {dex} | {pump_link}"
    else:
        links = _trade_links(mint, platform="pump")

    return (
        f"{header}\n\n"
        f"<b>{name} (${symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_trade_signal(mint: str, metadata: dict, score: int) -> str:
    """
    Formatter for clean bundle / super signal (trade_signals).
    metadata comes from get_saved_transaction_metadata().
    """
    name = metadata.get('name', 'Unknown') if metadata else 'Unknown'
    symbol = metadata.get('symbol', '?') if metadata else '?'
    twitter_url = metadata.get('twitter_url', '') if metadata else ''
    telegram_url = metadata.get('telegram_url', '') if metadata else ''
    website_url = metadata.get('website_url', '') if metadata else ''

    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint, platform="pump")

    return (
        f"⚡ <b>Super Signal — Clean Bundle!</b>\n\n"
        f"<b>{name} (${symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"🧹 Bundle Score: <b>{score}</b>\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_whale_large_trade(trade_data: dict, token_name: str, token_symbol: str, mint: str, channel_type: str) -> str:
    """
    Formatter for whale and large trades.
    trade_data comes from trigger_trade_alert() in pump_trades.py.
    """
    sol_amount = trade_data.get('sol_amount', 0)
    token_amount = trade_data.get('token_amount', 0)
    trade_type = trade_data.get('type', 'buy')
    signature = trade_data.get('signature', '')
    user = trade_data.get('user', '')

    action = "Bought" if trade_type == 'buy' else "Sold"
    emoji = "🐋" if channel_type == "whale_trades" else "🐟"
    header = f"{emoji} <b>{'Whale' if channel_type == 'whale_trades' else 'Large'} {action}!</b>"

    user_url = f"https://solscan.io/account/{user}"
    tx_url = f"https://solscan.io/tx/{signature}"
    links = _trade_links(mint, platform="pump")

    return (
        f"{header}\n\n"
        f"<b>{token_name} (${token_symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"💰 <b>{sol_amount:.2f} SOL</b> — {action} {token_amount:,.0f} tokens\n"
        f"👤 <a href=\"{user_url}\">Trader</a> | <a href=\"{tx_url}\">Tx</a>\n\n"
        f"{links}"
    )


def format_dexscreener_boost(mint: str, name: str, symbol: str, user: str, description: str,
                             twitter_url: str, telegram_url: str, website_url: str,
                             amount, signal_type: str) -> str:
    """
    Formatter for DexScreener boost signals (dexscreener_boosts and dexscreener_top_boosts).
    """
    header = "🔥 <b>Dex Most Boosted!</b>" if signal_type == "dexscreener_top_boosts" else "⚡ <b>New Dex Boost!</b>"

    if description and len(description) > 200:
        description = description[:197] + "..."

    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint)
    creator_line = f'👤 <a href="https://solscan.io/account/{user}">Creator</a>\n' if user else ""

    return (
        f"{header}\n\n"
        f"<b>{name} (${symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"📝 {description or 'No Description Added'}\n\n"
        f"{creator_line}"
        f"⚡ Boost Amount: <b>{amount}</b>\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_dexscreener_paid(mint: str, name: str, symbol: str, user: str, description: str,
                            twitter_url: str, telegram_url: str, website_url: str,
                            dex_url: str) -> str:
    """
    Formatter for DexScreener paid listing (dexscreener_updates).
    """
    if description and len(description) > 200:
        description = description[:197] + "..."

    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint)
    creator_line = f'👤 <a href="https://solscan.io/account/{user}">Creator</a>\n' if user else ""
    dex_line = f'📊 <a href="{dex_url}">Dex Profile</a>\n' if dex_url else ""

    return (
        f"💎 <b>Dex Paid!</b>\n\n"
        f"<b>{name} (${symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"📝 {description or 'No Description Added'}\n\n"
        f"{creator_line}"
        f"{dex_line}"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_pump_livestream(stream_data: dict) -> str:
    """
    Formatter for pump.fun livestreams (pump_livestreams signal).
    stream_data comes from trigger_livestream_alert() in pump_main.py.
    """
    mint = stream_data.get('mint', '')
    name = stream_data.get('name', 'Unknown')
    symbol = stream_data.get('symbol', '?')
    description = stream_data.get('description', 'No description')
    market_cap = stream_data.get('market_cap', 0)
    reply_count = stream_data.get('reply_count', 0)
    twitter = stream_data.get('twitter', '')
    telegram = stream_data.get('telegram', '')
    website = stream_data.get('website', '')

    if description and len(description) > 200:
        description = description[:197] + "..."

    socials = _socials_line(twitter, telegram, website)
    links = _trade_links(mint, platform="pump")

    return (
        f"🔴 <b>LIVE: {name} (${symbol})</b>\n\n"
        f"<code>{mint}</code>\n\n"
        f"📝 {description}\n\n"
        f"💰 MCap: <b>${market_cap:,.0f}</b>\n"
        f"💬 {reply_count} messages\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )
