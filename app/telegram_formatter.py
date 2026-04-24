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


def format_wallet_tracker(mint: str, token_name: str, token_symbol: str,
                          wallet_name: str, wallet_address: str, wallet_twitter: str,
                          trade_type: str, sol_amount: float, signature: str) -> str:
    """
    Formatter for tracked wallet trade alerts (wallet_tracker signal).
    """
    if trade_type == "BUY":
        action_emoji = "🟢"
        action = "Bought"
    else:
        action_emoji = "🔴"
        action = "Sold"

    wallet_url = f"https://solscan.io/account/{wallet_address}"
    tx_url = f"https://solscan.io/tx/{signature}"
    wallet_link = f'<a href="{wallet_twitter}">{wallet_name}</a>' if wallet_twitter else f'<a href="{wallet_url}">{wallet_name}</a>'
    links = _trade_links(mint)

    return (
        f"{action_emoji} <b>Tracked Wallet — {action}!</b>\n\n"
        f"<b>{token_name} (${token_symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"👛 {wallet_link} | <a href=\"{wallet_url}\">Solscan</a>\n"
        f"💰 <b>{sol_amount:.2f} SOL</b> {action}\n"
        f"🔗 <a href=\"{tx_url}\">Transaction</a>\n\n"
        f"{links}"
    )


def format_based_dev(creator: str, mint: str, token_name: str, token_symbol: str,
                     total_tokens: int, successful_tokens: int, performance_score: int,
                     twitter_url: str, telegram_url: str, website_url: str) -> str:
    """
    Formatter for based dev creator alerts (based_dev signal).
    """
    creator_url = f"https://solscan.io/account/{creator}"
    success_rate = int(successful_tokens / total_tokens * 100) if total_tokens > 0 else 0
    socials = _socials_line(twitter_url, telegram_url, website_url)
    links = _trade_links(mint)

    return (
        f"🔥 <b>Based Dev Alert!</b>\n\n"
        f"<b>{token_name} (${token_symbol})</b> — latest token\n"
        f"<code>{mint}</code>\n\n"
        f"👤 <a href=\"{creator_url}\">Creator</a>\n"
        f"🏆 Score: <b>{performance_score}</b> | {successful_tokens}/{total_tokens} bonded ({success_rate}%)\n"
        f"🔗 {socials}\n\n"
        f"{links}"
    )


def format_bonk_bonding_curve(mint: str, stage: str, token_name: str, token_symbol: str,
                               creator: str, twitter_url: str, telegram_url: str,
                               website_url: str, progress: float) -> str:
    """
    Formatter for bonk.fun bonding curve milestones (80% and complete).
    """
    if stage == "80percent":
        header = f"🔥 <b>Bonk 80% Bonding Curve!</b>"
    else:
        header = f"💎 <b>Bonk 100% — Migrated to Raydium!</b>"

    creator_url = f"https://solscan.io/account/{creator}" if creator else ""
    creator_line = f'👤 <a href="{creator_url}">Creator</a>\n' if creator else ""
    socials = _socials_line(twitter_url, telegram_url, website_url)

    photon = f'<a href="https://photon-sol.tinyastro.io/en/r/@codesaga/{mint}">Photon</a>'
    axiom = f'<a href="https://axiom.trade/t/{mint}/@codesaga">Axiom</a>'
    dex = f'<a href="https://dexscreener.com/solana/{mint}">Dex</a>'
    bonk = f'<a href="https://bonk.fun/token/{mint}">Bonk</a>'
    links = f"{photon} | {axiom} | {dex} | {bonk}"

    return (
        f"{header}\n\n"
        f"<b>{token_name} (${token_symbol})</b>\n"
        f"<code>{mint}</code>\n\n"
        f"📊 Progress: <b>{progress:.1f}%</b>\n"
        f"{creator_line}"
        f"🔗 {socials}\n\n"
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


def format_polymarket_alert(title: str, question: str, consensus: str,
                            stats: str, crypto_impact: str, url: str,
                            tier_label: str) -> str:
    """
    Formatter for Polymarket ALERT and SIGNAL tier signals
    (polymarket_alerts and polymarket_signals topics).
    """
    emoji = "🔴" if tier_label == "ALERT" else "🟡"
    impact_section = f"\n📡 <b>Crypto Impact</b>\n{crypto_impact}\n" if crypto_impact else ""

    return (
        f"{emoji} <b>[{tier_label}] {title}</b>\n\n"
        f"<i>{question}</i>\n\n"
        f"📊 {consensus}\n\n"
        f"{stats}"
        f"{impact_section}\n"
        f'<a href="{url}">View on Polymarket</a>'
    )


def format_polymarket_signal(title: str, question: str, consensus: str,
                             stats: str, read: str, url: str) -> str:
    """
    Formatter for Polymarket SIGNAL tier (macro, consensus, airdrop intel).
    """
    return (
        f"🟡 <b>{title}</b>\n\n"
        f"<i>{question}</i>\n\n"
        f"📊 {consensus}\n\n"
        f"{stats}"
        f"💡 {read}\n\n"
        f'<a href="{url}">View on Polymarket</a>'
    )


def format_polymarket_daily(movers: list, top_volume: list, resolving: list,
                             signal_count: int, market_count: int) -> str:
    """
    Formatter for the Polymarket daily digest (polymarket_daily topic).
    movers, top_volume, resolving are lists of market dicts from the Gamma API.
    """
    from polymarket_monitor import _top, _pct, _sp, _usd, _days_until, CU, CD

    lines = [f"📊 <b>Polymarket Daily Digest</b>\n"]
    lines.append(f"<i>{signal_count} signals fired · {market_count} markets tracked</i>\n")

    # Biggest movers
    if movers:
        lines.append("\n<b>📈 Biggest Movers</b>")
        for m in movers[:5]:
            ch = m.get("oneDayPriceChange") or 0
            arrow = "▲" if ch > 0 else "▼"
            price, oc = _top(m)
            q = (m.get("question") or "")[:45]
            slug = m.get("slug", "")
            url = f"https://polymarket.com/event/{slug}"
            lines.append(f'{arrow} {_sp(ch)}  <a href="{url}">{q}</a> → {oc} {_pct(price)}')

    # Top volume
    if top_volume:
        lines.append("\n<b>💰 Top Volume (24h)</b>")
        for m in top_volume[:5]:
            vol = m.get("volume24hr") or 0
            price, oc = _top(m)
            q = (m.get("question") or "")[:45]
            slug = m.get("slug", "")
            url = f"https://polymarket.com/event/{slug}"
            lines.append(f'{_usd(vol)}  <a href="{url}">{q}</a> → {oc} {_pct(price)}')

    # Resolving soon
    if resolving:
        lines.append("\n<b>⏰ Resolving Soon</b>")
        for m in resolving[:4]:
            d = _days_until(m.get("endDate") or m.get("endDateIso"))
            price, oc = _top(m)
            q = (m.get("question") or "")[:40]
            slug = m.get("slug", "")
            url = f"https://polymarket.com/event/{slug}"
            lines.append(f'{d:.0f}d  <a href="{url}">{q}</a> → {oc} {_pct(price)}')

    return "\n".join(lines)


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
