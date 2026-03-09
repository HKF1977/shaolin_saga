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
