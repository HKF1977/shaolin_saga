"""
Polymarket Monitor — Shaolin Saga
==================================
Polls the Polymarket Gamma API every 2 minutes, runs signal detectors,
and routes results to the appropriate Discord channels and Telegram topics.

Signal tiers:
    ALERT  — volume surges, odds reversals          → oracle_alerts  (HIGH priority)
    SIGNAL — macro, consensus, velocity, early, TGE → oracle_signals (MEDIUM priority)
    WATCH  — resolution plays, contrarian setups    → oracle_plays   (LOW priority)
    DAILY  — digest at configurable UTC hour        → oracle_daily   (LOW priority)

Cooldowns are change-based: a market re-fires only when odds have moved
another threshold since the last signal, so fast-moving markets stay
covered without spamming.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord

from rate_limiter import queue_discord_send, MessagePriority
from telegram_sender import queue_telegram_send, get_telegram_targets
from telegram_formatter import format_polymarket_daily
from utils import safe_json_read, safe_json_write

STATE_PATH = "/home/shaolin_saga/data/polymarket/polymarket_state.json"
GAMMA = "https://gamma-api.polymarket.com"


# ── TIERS ─────────────────────────────────────────────────────────────────────

class Tier(IntEnum):
    WATCH = 1
    SIGNAL = 2
    ALERT = 3


TIER_CFG = {
    Tier.ALERT:  {"label": "ALERT",  "emoji": "\U0001f534", "colour": 0xC0392B, "priority": MessagePriority.HIGH},
    Tier.SIGNAL: {"label": "SIGNAL", "emoji": "\U0001f7e1", "colour": 0xC8A84E, "priority": MessagePriority.MEDIUM},
    Tier.WATCH:  {"label": "WATCH",  "emoji": "\U0001f7e2", "colour": 0x57F287, "priority": MessagePriority.LOW},
}

# Channel key for each destination
CHANNEL_FOR_TIER = {
    "alerts": "oracle_alerts",
    "signals": "oracle_signals",
    "plays": "oracle_plays",
    "daily": "oracle_daily",
}

# Telegram signal types for each destination
TELEGRAM_SIGNAL = {
    "alerts": "polymarket_alerts",
    "signals": "polymarket_signals",
    "plays": None,   # WATCH tier — skip Telegram
    "daily": "polymarket_daily",
}


# ── MACRO KEYWORDS → CRYPTO IMPACT ───────────────────────────────────────────

MACRO: Dict[str, Tuple[str, str]] = {
    "fed":           ("BTC, ETH, risk assets",           "Rate cuts → bullish, holds/hikes → bearish"),
    "interest rate": ("BTC, ETH, risk assets",           "Cuts bullish, hikes bearish"),
    "iran":          ("BTC, oil plays",                  "Escalation → short-term bearish, flight to safety"),
    "israel":        ("BTC, oil, defence tokens",        "Conflict escalation → volatility spike"),
    "ukraine":       ("Energy tokens, BTC",              "Escalation → risk-off, ceasefire → risk-on"),
    "russia":        ("Energy, sanctions-exposed tokens", "Sanctions → volatility"),
    "tariff":        ("All risk assets, DeFi",           "New tariffs → bearish, removal → bullish"),
    "recession":     ("BTC, stablecoins, DeFi yields",   "Recession fears → risk-off, then BTC narrative"),
    "sec":           ("Altcoins, DeFi tokens",           "Enforcement → bearish for named tokens"),
    "etf":           ("BTC, ETH, SOL",                   "Approval → bullish, rejection → bearish"),
    "regulation":    ("DeFi, CEX tokens, stablecoins",   "Clarity → bullish, crackdowns → bearish"),
    "trump":         ("Crypto broadly, DeFi",            "Pro-crypto policy → bullish"),
    "inflation":     ("BTC, stablecoins",                "High inflation → BTC hedge narrative strengthens"),
    "war":           ("BTC, defence, oil",               "New conflict → volatility spike, safe haven flows"),
    "election":      ("Regulation-sensitive tokens",     "Pro-crypto candidate → bullish"),
    "china":         ("BTC, mining tokens",              "Ban → bearish, easing → bullish"),
}

CRYPTO_KW = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "bnb",
    "dogecoin", "doge", "hyperliquid", "crypto", "defi",
]
AIRDROP_KW = ["airdrop", "token launch", "tge", "listing"]
PRICE_KW = ["price", "hit", "above", "below", "reach", "exceed"]

UP = "\U0001f53c"
DN = "\U0001f53b"
CU = "\U0001f4c8"
CD = "\U0001f4c9"


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _market_url(m: dict) -> str:
    """
    Build the best available Polymarket URL for a market dict.
    Prefers the parent event slug from events[0] over the market-level slug,
    which avoids linking to individual game markets (e.g. -game1) instead of
    the parent match event page.
    """
    events = m.get("events") or []
    if events and isinstance(events, list):
        parent_slug = events[0].get("slug", "") if isinstance(events[0], dict) else ""
        if parent_slug:
            return f"https://polymarket.com/event/{parent_slug}"

    slug = m.get("slug", "")
    if not slug:
        return "https://polymarket.com"
    return f"https://polymarket.com/event/{slug}"


def _parse_prices(m: dict) -> List[float]:
    try:
        return [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    except Exception:
        return []


def _parse_outcomes(m: dict) -> List[str]:
    try:
        return json.loads(m.get("outcomes") or "[]")
    except Exception:
        return []


def _top(m: dict) -> Tuple[float, str]:
    prices, outcomes = _parse_prices(m), _parse_outcomes(m)
    if not prices:
        return 0.0, "\u2014"
    mx = max(prices)
    idx = prices.index(mx)
    return mx, outcomes[idx] if idx < len(outcomes) else "Yes"


def _usd(n: Optional[float]) -> str:
    if not n:
        return "$0"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


def _pct(n: float) -> str:
    return f"{n * 100:.1f}%"


def _sp(n: float) -> str:
    return f"{'+' if n > 0 else ''}{n * 100:.1f}%"


def _hours_ago(s: Optional[str]) -> float:
    if not s:
        return float("inf")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return float("inf")


def _days_until(s: Optional[str]) -> float:
    if not s:
        return float("inf")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return float("inf")


def _is_crypto(q: str) -> bool:
    return any(k in q for k in CRYPTO_KW)


def _is_price_market(q: str) -> bool:
    return any(k in q for k in CRYPTO_KW) and any(w in q for w in PRICE_KW)


def _is_airdrop(q: str) -> bool:
    return any(k in q for k in AIRDROP_KW)


def _macro_hit(q: str) -> Optional[Tuple[str, Tuple[str, str]]]:
    return next(((kw, ctx) for kw, ctx in MACRO.items() if kw in q), None)


def _crypto_tag(q: str) -> Optional[str]:
    hit = _macro_hit(q)
    if hit:
        _, (assets, direction) = hit
        return f"Assets: {assets}\nRead:   {direction}"
    if _is_crypto(q):
        return "Crypto-relevant market"
    return None


# ── MONITOR CLASS ─────────────────────────────────────────────────────────────

class PolymarketMonitor:
    """
    Polls Polymarket every scan_interval seconds, runs detectors,
    and posts signals to Discord + Telegram.
    """

    def __init__(self, bot: discord.Client, servers: dict, logger: logging.Logger,
                 icon_url: str = "", scan_interval: int = 120, digest_hour_utc: int = 21):
        self.bot = bot
        self.servers = servers
        self.logger = logger
        self.icon_url = icon_url
        self.scan_interval = scan_interval
        self.digest_hour_utc = digest_hour_utc
        self.session: Optional[aiohttp.ClientSession] = None

        # In-memory state
        self._last_sig: Dict[str, dict] = {}
        self._price_snaps: Dict[str, List[dict]] = {}
        self._vol_snaps: Dict[str, List[dict]] = {}
        self._today: List[discord.Embed] = []
        self._last_digest: Optional[str] = None
        self._n: int = 0

        self._load_state()

    # ── STATE PERSISTENCE ──────────────────────────────────────────────────

    def _load_state(self):
        """Load cooldown state from disk so restarts don't re-fire old signals."""
        raw = safe_json_read(STATE_PATH, {})
        sigs = raw.get("last_sig", {})
        for key, val in sigs.items():
            try:
                self._last_sig[key] = {
                    "p": float(val["p"]),
                    "t": datetime.fromisoformat(val["t"]),
                }
            except Exception:
                pass
        self._last_digest = raw.get("last_digest")
        self.logger.info("Polymarket: loaded %d cooldown entries from disk", len(self._last_sig))

    def _save_state(self):
        """Persist cooldown state to disk."""
        serialised = {
            k: {"p": v["p"], "t": v["t"].isoformat()}
            for k, v in self._last_sig.items()
        }
        safe_json_write(STATE_PATH, {
            "last_sig": serialised,
            "last_digest": self._last_digest,
        })

    # ── LIFECYCLE ──────────────────────────────────────────────────────────

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.logger.info("Polymarket monitor started — scan every %ds", self.scan_interval)
        asyncio.create_task(self._scan_loop())
        asyncio.create_task(self._digest_loop())

    async def stop(self):
        if self.session:
            await self.session.close()

    # ── EMBED BUILDER ──────────────────────────────────────────────────────

    def _embed(self, title: str, tier: Tier, m: Optional[dict] = None) -> discord.Embed:
        cfg = TIER_CFG[tier]
        e = discord.Embed(
            title=f"{cfg['emoji']} {title}",
            colour=cfg["colour"],
            url=_market_url(m) if m else None,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_author(name="Shaolin Saga", icon_url=self.icon_url)
        e.set_footer(text="Powered by Shaolin Saga!", icon_url=self.icon_url)
        if m:
            img = m.get("image")
            if img:
                e.set_thumbnail(url=img)
        return e

    # ── API ────────────────────────────────────────────────────────────────

    async def _fetch_page(self, offset: int = 0) -> List[dict]:
        url = (f"{GAMMA}/markets?limit=100&offset={offset}"
               "&active=true&closed=false&order=volume24hr&ascending=false")
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.json() if r.status == 200 else []
        except Exception as e:
            self.logger.warning("Polymarket API error (offset %d): %s", offset, e)
            return []

    async def _fetch_all(self) -> List[dict]:
        out: List[dict] = []
        for page in range(15):
            batch = await self._fetch_page(page * 100)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            await asyncio.sleep(0.15)
        return out

    # ── CHANGE-BASED COOLDOWNS ─────────────────────────────────────────────

    def _ok(self, slug: str, det: str, price: float, threshold: float = 0.10) -> bool:
        key = f"{slug}:{det}"
        last = self._last_sig.get(key)
        if not last:
            return True
        return abs(price - last["p"]) >= threshold

    def _mark(self, slug: str, det: str, price: float):
        self._last_sig[f"{slug}:{det}"] = {
            "p": price,
            "t": datetime.now(timezone.utc),
        }

    # ── SNAPSHOTS ──────────────────────────────────────────────────────────

    def _snap(self, m: dict):
        slug = m.get("slug")
        if not slug:
            return
        now = datetime.now(timezone.utc)
        price, _ = _top(m)
        if price:
            s = self._price_snaps.setdefault(slug, [])
            s.append({"p": price, "t": now})
            if len(s) > 200:
                del s[:len(s) - 200]
        vol = m.get("volume24hr") or 0
        sv = self._vol_snaps.setdefault(slug, [])
        sv.append({"v": vol, "v1wk": m.get("volume1wk") or 0, "t": now})
        if len(sv) > 200:
            del sv[:len(sv) - 200]

    def _velocity(self, slug: str, m: Optional[dict] = None, hours: int = 6) -> Optional[dict]:
        """
        Get price velocity over recent snapshot window.
        Falls back to oneHourPriceChange from the API response when there
        aren't enough snapshots yet (e.g. shortly after startup).
        """
        snaps = self._price_snaps.get(slug)
        if snaps and len(snaps) >= 2:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            w = [s for s in snaps if s["t"] >= cutoff]
            if len(w) >= 2:
                d = w[-1]["p"] - w[0]["p"]
                h = (w[-1]["t"] - w[0]["t"]).total_seconds() / 3600
                if h >= 0.01:
                    return {"d": d, "h": h, "f": w[0]["p"], "t": w[-1]["p"], "source": "snaps"}

        # Warm-start fallback — use API's own 1h change field
        if m is not None:
            ch1h = m.get("oneHourPriceChange")
            price, _ = _top(m)
            if ch1h is not None and price:
                return {"d": float(ch1h), "h": 1.0, "f": price - float(ch1h), "t": price, "source": "api"}
        return None

    def _vol_rate(self, m: dict) -> Optional[Tuple[float, float]]:
        """
        Returns (vol_24h, avg_daily_vol) or None.

        Uses volume24hr directly as the current signal and volume1wk/7 as the
        stable weekly baseline. Avoids the rolling-window-delta approach which
        produced unreliable results because volume24hr is not a counter — it's
        a sliding window that can decrease when old trades fall off.
        """
        vol24h = m.get("volume24hr") or 0
        vol1wk = m.get("volume1wk") or 0
        if vol24h < 1 or vol1wk < 7:
            return None
        avg_daily = vol1wk / 7
        if avg_daily < 1_000:
            return None
        return vol24h, avg_daily

    def _cleanup(self):
        now = datetime.now(timezone.utc)
        cut = now - timedelta(hours=24)
        self._last_sig = {k: v for k, v in self._last_sig.items() if v["t"] > cut}
        for store in (self._price_snaps, self._vol_snaps):
            for slug in list(store):
                filtered = [s for s in store[slug] if s["t"] > cut]
                if filtered:
                    store[slug] = filtered
                else:
                    del store[slug]

    # ═══════════════════════════════════════════════════════════════════════
    #  DETECTORS
    # ═══════════════════════════════════════════════════════════════════════

    def _d_vsurge(self, mks: List[dict]) -> List[tuple]:
        """ALERT: Volume surge — today's volume 5x+ the weekly daily average."""
        out = []
        for m in mks:
            slug = m.get("slug", "")
            vol = m.get("volume24hr") or 0
            liq = m.get("liquidityNum") or 0
            if vol < 10_000 or liq < 2_000:
                continue
            rates = self._vol_rate(m)
            if not rates:
                continue
            cur, avg = rates
            mult = cur / avg
            if mult < 5:
                continue
            last_vsurge = self._last_sig.get(f"{slug}:vsurge")
            if last_vsurge and (datetime.now(timezone.utc) - last_vsurge["t"]).total_seconds() < 7200:
                continue
            price, oc = _top(m)
            ch = m.get("oneDayPriceChange") or 0
            q = (m.get("question") or "").lower()

            e = self._embed(f"VOLUME SURGE — {m.get('question', '')[:55]}", Tier.ALERT, m)
            e.add_field(
                name="Consensus",
                value=f"```{oc} at {_pct(price)}  {UP if ch > 0 else DN} {_sp(ch)} (24h)```",
                inline=False,
            )
            e.add_field(name="\u26a1 Vol (24h)", value=f"**{_usd(cur)}**", inline=True)
            e.add_field(name="\u26a1 Avg Vol/day", value=f"**{_usd(avg)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            e.add_field(name="Surge", value=f"```{mult:.1f}x avg daily volume```", inline=False)
            ct = _crypto_tag(q)
            if ct:
                e.add_field(name="Crypto Impact", value=f"```\n{ct}```", inline=False)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            out.append(("alerts", Tier.ALERT, e, m))
            self._mark(slug, "vsurge", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:6]

    def _d_reversal(self, mks: List[dict]) -> List[tuple]:
        """ALERT: Odds reversal — direction flip with 15%+ swing."""
        out = []
        for m in mks:
            slug = m.get("slug", "")
            vol = m.get("volume24hr") or 0
            liq = m.get("liquidityNum") or 0
            if vol < 20_000 or liq < 2_000:
                continue
            snaps = self._price_snaps.get(slug)
            if not snaps or len(snaps) < 10:
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
            w = [s for s in snaps if s["t"] >= cutoff]
            if len(w) < 5:
                continue
            prices = [s["p"] for s in w]

            # Find the actual local peak and trough rather than using the
            # midpoint, so swing magnitude and pivot price are accurate.
            peak_idx = prices.index(max(prices))
            trough_idx = prices.index(min(prices))

            # Peak reversal: climbed to a real high then came back down.
            # Trough reversal: dropped to a real low then bounced back up.
            # The pivot must not be at either end of the window (otherwise
            # it's just a continuing move, not a reversal).
            best = None
            if 0 < peak_idx < len(prices) - 1:
                first = prices[peak_idx] - prices[0]
                second = prices[-1] - prices[peak_idx]
                if first > 0 and second < 0:
                    best = (first, second, peak_idx, "climbed then reversed down")
            if 0 < trough_idx < len(prices) - 1:
                first = prices[trough_idx] - prices[0]
                second = prices[-1] - prices[trough_idx]
                if first < 0 and second > 0:
                    candidate = (first, second, trough_idx, "dropped then reversed up")
                    if best is None or abs(first) + abs(second) > abs(best[0]) + abs(best[1]):
                        best = candidate

            if not best:
                continue
            first, second, pivot_idx, direction = best
            swing = abs(first) + abs(second)
            if swing < 0.15:
                continue
            price, oc = _top(m)
            if not self._ok(slug, "reversal", price, 0.12):
                continue
            q = (m.get("question") or "").lower()
            tier = Tier.ALERT if swing >= 0.20 and vol >= 50_000 else Tier.SIGNAL

            e = self._embed(f"ODDS REVERSAL — {m.get('question', '')[:50]}", tier, m)
            e.add_field(
                name="Reversal",
                value=(f"```{direction}\n"
                       f"{_pct(prices[0])} → {_pct(prices[pivot_idx])} → "
                       f"{_pct(prices[-1])} ({_sp(second)})```"),
                inline=False,
            )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            ct = _crypto_tag(q)
            if ct:
                e.add_field(name="Crypto Impact", value=f"```\n{ct}```", inline=False)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            ch_name = "alerts" if tier == Tier.ALERT else "signals"
            out.append((ch_name, tier, e, m))
            self._mark(slug, "reversal", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:5]

    def _d_macro(self, mks: List[dict]) -> List[tuple]:
        """SIGNAL: Macro movers with crypto impact."""
        out = []
        for m in mks:
            q = (m.get("question") or "").lower()
            vol = m.get("volume24hr") or 0
            liq = m.get("liquidityNum") or 0
            ch = m.get("oneDayPriceChange") or 0
            ac = abs(ch)
            if liq < 2_000 or vol < 1_000:
                continue
            hit = _macro_hit(q)
            if not hit or _is_price_market(q):
                continue
            price, oc = _top(m)
            tier = (Tier.ALERT if ac >= 0.15 and vol >= 50_000
                    else Tier.SIGNAL if ac >= 0.10 and vol >= 20_000
                    else Tier.WATCH if ac >= 0.05 and vol >= 5_000
                    else None)
            if not tier:
                continue
            slug = m.get("slug", "")
            if not self._ok(slug, "macro", price, 0.08):
                continue
            _, (assets, direction) = hit

            e = self._embed(m.get("question", ""), tier, m)
            e.add_field(
                name="Consensus",
                value=f"```{oc} at {_pct(price)}  {UP if ch > 0 else DN} {_sp(ch)} (24h)```",
                inline=False,
            )
            e.add_field(
                name="Crypto Impact",
                value=f"```\nAssets: {assets}\nRead:   {direction}```",
                inline=False,
            )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            channel = "alerts" if tier == Tier.ALERT else "signals"
            out.append((channel, tier, e, m))
            self._mark(slug, "macro", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:8]

    def _d_consensus(self, mks: List[dict]) -> List[tuple]:
        """SIGNAL: Crypto price market consensus shifts."""
        out = []
        for m in mks:
            q = (m.get("question") or "").lower()
            if not _is_price_market(q):
                continue
            liq = m.get("liquidityNum") or 0
            if liq < 2_000:
                continue
            price, oc = _top(m)
            vol = m.get("volume24hr") or 0
            ch = m.get("oneDayPriceChange") or 0
            ac = abs(ch)
            if ac < 0.05 or vol < 5_000:
                continue
            slug = m.get("slug", "")
            if not self._ok(slug, "consensus", price, 0.08):
                continue
            asset_map = [
                ("ethereum", "ETH"), (" eth ", "ETH"), ("solana", "SOL"),
                (" sol ", "SOL"), ("xrp", "XRP"), ("bnb", "BNB"),
                ("doge", "DOGE"), ("hyperliquid", "HYPE"),
            ]
            asset = next((a for k, a in asset_map if k in q), "BTC")
            tier = (Tier.ALERT if ac >= 0.15 and vol >= 50_000
                    else Tier.SIGNAL if ac >= 0.10 and vol >= 20_000
                    else Tier.WATCH)
            feel = "getting more bullish" if ch > 0 else "getting more bearish"
            vnote = ("Heavy volume confirms conviction." if vol > 100_000
                     else "Watch for volume confirmation.")

            e = self._embed(f"Price Consensus — {asset}", tier, m)
            e.add_field(name="Market", value=f"```{m.get('question', '')}```", inline=False)
            e.add_field(
                name="Consensus",
                value=f"```{oc} at {_pct(price)}  {UP if ch > 0 else DN} {_sp(ch)} (24h)```",
                inline=False,
            )
            vel = self._velocity(slug, m)
            if vel:
                sp = abs(vel["d"]) / vel["h"] * 100
                e.add_field(
                    name="Velocity",
                    value=(f"```{_sp(vel['d'])} in {vel['h']:.1f}h  "
                           f"({sp:.1f}%/hr)  from {_pct(vel['f'])}```"),
                    inline=False,
                )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            e.add_field(
                name="Read",
                value=f"```\nPolymarket crowd {feel} on {asset}. {vnote}```",
                inline=False,
            )
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            channel = "alerts" if tier == Tier.ALERT else "signals"
            out.append((channel, tier, e, m))
            self._mark(slug, "consensus", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:6]

    def _d_velocity(self, mks: List[dict]) -> List[tuple]:
        """SIGNAL: Fast odds movement detected."""
        out = []
        for m in mks:
            liq = m.get("liquidityNum") or 0
            vol = m.get("volume24hr") or 0
            if liq < 2_000 or vol < 1_000:
                continue
            slug = m.get("slug", "")
            q = (m.get("question") or "").lower()
            if _is_price_market(q):
                continue
            vel = self._velocity(slug, m)
            if not vel:
                continue
            ad = abs(vel["d"])
            tier = (Tier.ALERT if ad >= 0.20
                    else Tier.SIGNAL if ad >= 0.12
                    else Tier.WATCH if ad >= 0.08
                    else None)
            if not tier:
                continue
            price, oc = _top(m)
            if not self._ok(slug, "velocity", price, 0.10):
                continue
            sp = ad / vel["h"] * 100
            icon = "\U0001f680" if vel["d"] > 0 else "\U0001f4a5"

            e = self._embed(f"{icon} {(m.get('question') or '')[:55]}", tier, m)
            e.add_field(
                name="Velocity",
                value=f"```{_sp(vel['d'])} in {vel['h']:.1f}h  ({sp:.1f}%/hr)```",
                inline=False,
            )
            e.add_field(
                name="Consensus",
                value=f"```{oc} at {_pct(price)}  (was {_pct(vel['f'])})```",
                inline=False,
            )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            ct = _crypto_tag(q)
            if ct:
                e.add_field(name="Crypto Impact", value=f"```\n{ct}```", inline=False)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            channel = "alerts" if tier == Tier.ALERT else "signals"
            out.append((channel, tier, e, m))
            self._mark(slug, "velocity", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:6]

    def _d_early(self, mks: List[dict]) -> List[tuple]:
        """SIGNAL: New markets under 36h old gaining traction."""
        out = []
        for m in mks:
            age = _hours_ago(m.get("createdAt"))
            if age > 36:
                continue
            vol = m.get("volume24hr") or 0
            if vol < 3_000:
                continue
            slug = m.get("slug", "")
            price, oc = _top(m)
            if not self._ok(slug, "early", price, 0.10):
                continue
            q = (m.get("question") or "").lower()
            crypto = _is_crypto(q) or _is_airdrop(q) or bool(_macro_hit(q))
            tier = (Tier.ALERT if vol >= 50_000 and age < 12
                    else Tier.SIGNAL if vol >= 20_000 and age < 24
                    else Tier.WATCH)
            age_s = "<1 hour" if age < 1 else f"{int(age)} hours"

            e = self._embed("New Market" + (" — Crypto Relevant" if crypto else ""), tier, m)
            e.add_field(name="Market", value=f"```{m.get('question', '')}```", inline=False)
            e.add_field(
                name="Early Odds",
                value=f"```{oc} at {_pct(price)}  \u23f1\ufe0f {age_s} old```",
                inline=False,
            )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(m.get('liquidityNum', 0))}**", inline=True)
            tags = []
            if crypto:
                tags.append("\u2705 Crypto-relevant")
            if vol >= 10_000:
                tags.append("\u2705 Fast traction")
            if tags:
                e.add_field(name="Indicators", value="  ".join(tags), inline=False)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            channel = "alerts" if tier == Tier.ALERT else "signals"
            out.append((channel, tier, e, m))
            self._mark(slug, "early", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:5]

    def _d_airdrop(self, mks: List[dict]) -> List[tuple]:
        """SIGNAL: Airdrop / TGE / token launch markets."""
        out = []
        for m in mks:
            q = (m.get("question") or "").lower()
            if not _is_airdrop(q):
                continue
            vol = m.get("volume24hr") or 0
            liq = m.get("liquidityNum") or 0
            ch = m.get("oneDayPriceChange") or 0
            ac = abs(ch)
            if liq < 500 or vol < 500 or (ac < 0.05 and vol < 10_000):
                continue
            slug = m.get("slug", "")
            price, oc = _top(m)
            if not self._ok(slug, "airdrop", price, 0.10):
                continue
            days = _days_until(m.get("endDate") or m.get("endDateIso"))
            tier = (Tier.ALERT if ac >= 0.20 and vol >= 100_000
                    else Tier.SIGNAL if ac >= 0.15 or vol >= 50_000
                    else Tier.WATCH)
            if price > 0.7:
                alpha = (f"Market strongly expects this. Position accordingly "
                         f"— {100 - price * 100:.0f}¢ upside if wrong.")
            elif price < 0.3:
                alpha = ("Market is skeptical. Contrarian opportunity if you "
                         "have reason to believe otherwise.")
            else:
                alpha = "Market is split — watch for conviction to build."

            e = self._embed("Airdrop Intel", tier, m)
            e.add_field(name="Market", value=f"```{m.get('question', '')}```", inline=False)
            e.add_field(
                name="Consensus",
                value=f"```{oc} at {_pct(price)}  {UP if ch > 0 else DN} {_sp(ch)} (24h)```",
                inline=False,
            )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            if days < float("inf"):
                e.add_field(name="\U0001f4c5 Deadline", value=f"**{days:.0f} days**", inline=True)
            e.add_field(name="Alpha", value=f"```\n{alpha}```", inline=False)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            out.append(("signals", tier, e, m))
            self._mark(slug, "airdrop", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:5]

    def _d_resolution(self, mks: List[dict]) -> List[tuple]:
        """PLAYS: High-probability markets resolving within 7 days."""
        plays = []
        for m in mks:
            price, oc = _top(m)
            if price < 0.90:
                continue
            days = _days_until(m.get("endDate") or m.get("endDateIso"))
            if days <= 0 or days > 7:
                continue
            if (m.get("liquidityNum") or 0) < 2_000:
                continue
            slug = m.get("slug", "")
            if not self._ok(slug, "resolution", price, 0.05):
                continue
            plays.append(m)
            self._mark(slug, "resolution", price)

        if not plays:
            return []

        plays = sorted(
            plays,
            key=lambda m: _days_until(m.get("endDate") or m.get("endDateIso")),
        )[:6]
        lines = []
        for m in plays:
            p, oc = _top(m)
            d = _days_until(m.get("endDate") or m.get("endDateIso"))
            liq = m.get("liquidityNum") or 0
            q = (m.get("question") or "")[:40]
            lines.append(f"{_pct(p)}  {d:.0f}d  {_usd(liq)} liq  {q}")

        e = self._embed(f"Resolution play — {len(plays)} markets", Tier.WATCH, plays[0])
        e.add_field(name="Markets", value="```\n" + "\n".join(lines) + "```", inline=False)
        e.add_field(
            name="Read",
            value=("```\nHigh-probability markets resolving soon. Buy YES at "
                   "current odds for small, consistent returns. Check liquidity "
                   "before sizing.```"),
            inline=False,
        )
        e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(plays[0])})", inline=False)
        return [("plays", Tier.WATCH, e, plays[0])]

    def _d_contrarian(self, mks: List[dict]) -> List[tuple]:
        """PLAYS: Sharp drops on moderate volume — potential contrarian value."""
        out = []
        for m in mks:
            ch = m.get("oneDayPriceChange") or 0
            if ch > -0.10:
                continue
            vol = m.get("volume24hr") or 0
            liq = m.get("liquidityNum") or 0
            if liq < 2_000 or vol < 5_000:
                continue
            price, oc = _top(m)
            slug = m.get("slug", "")
            if not self._ok(slug, "contrarian", price, 0.10):
                continue
            q = (m.get("question") or "").lower()
            tier = Tier.SIGNAL if abs(ch) >= 0.15 and vol < 50_000 else Tier.WATCH

            e = self._embed(f"Contrarian Setup — {m.get('question', '')[:48]}", tier, m)
            e.add_field(
                name="Consensus",
                value=f"```{oc} at {_pct(price)}  {DN} {_sp(ch)} (24h)```",
                inline=False,
            )
            e.add_field(name="\U0001f4b0 Vol 24h", value=f"**{_usd(vol)}**", inline=True)
            e.add_field(name="\U0001f4a7 Liquidity", value=f"**{_usd(liq)}**", inline=True)
            e.add_field(
                name="Read",
                value=("```\nSharp drop on moderate volume. Could be panic selling "
                       "or new information. If you disagree with the move, this is "
                       "where the value lives.```"),
                inline=False,
            )
            ct = _crypto_tag(q)
            if ct:
                e.add_field(name="Crypto Impact", value=f"```\n{ct}```", inline=False)
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(m)})", inline=False)
            out.append(("plays", tier, e, m))
            self._mark(slug, "contrarian", price)
        return sorted(out, key=lambda x: x[1], reverse=True)[:4]

    def _d_convergence(self, mks: List[dict]) -> List[tuple]:
        """PLAYS: Multiple markets in same category moving the same direction."""
        out: List[tuple] = []
        cats: Dict[str, List[dict]] = {}
        for m in mks:
            if (m.get("volume24hr") or 0) < 50_000:
                continue
            cats.setdefault((m.get("category") or "general").lower(), []).append(m)

        for cat, ms in cats.items():
            movers = [m for m in ms if abs(m.get("oneDayPriceChange") or 0) >= 0.05]
            if len(movers) < 2:
                continue
            bull = sum(1 for m in movers if (m.get("oneDayPriceChange") or 0) > 0)
            dom = "bullish" if bull > len(movers) / 2 else "bearish"
            aln = max(bull, len(movers) - bull) / len(movers)
            if aln < 0.6:
                continue
            if not self._ok(cat, "convergence", aln, 0.15):
                continue
            tvol = sum(m.get("volume24hr") or 0 for m in movers)
            tier = (Tier.ALERT if tvol >= 500_000 and aln >= 0.8
                    else Tier.SIGNAL if tvol >= 200_000 and aln >= 0.7
                    else Tier.WATCH)
            top3 = sorted(movers, key=lambda m: m.get("volume24hr") or 0, reverse=True)[:3]
            lines = "\n".join(
                f"{CU if (m.get('oneDayPriceChange') or 0) > 0 else CD} "
                f"{_sp(m.get('oneDayPriceChange') or 0)}  "
                f"{(m.get('question') or '')[:42]}"
                for m in top3
            )

            e = self._embed(f"{len(movers)} {cat} markets shifting {dom}", tier, top3[0])
            e.add_field(
                name="Pattern",
                value=(f"```{len(movers)} markets  {dom}  "
                       f"{aln * 100:.0f}% aligned  {_usd(tvol)} combined```"),
                inline=False,
            )
            e.add_field(name="Top Movers", value=f"```\n{lines}```", inline=False)
            e.add_field(
                name="Read",
                value=(f"```\nMultiple {cat} markets converging {dom}. Crowd money "
                       f"aligning across related markets often precedes real movement.```"),
                inline=False,
            )
            e.add_field(name="Links", value=f"[POLYMARKET]({_market_url(top3[0])})", inline=False)
            out.append(("plays", tier, e, top3[0]))
            self._mark(cat, "convergence", aln)
        return sorted(out, key=lambda x: x[1], reverse=True)[:3]

    # ── POSTING ────────────────────────────────────────────────────────────

    async def _post(self, sigs: List[tuple]):
        """Route signals to the correct Discord channels and Telegram topics."""
        for server in self.servers.get("allowed_servers", []):
            channels = server.get("channels", {})
            for dest, tier, embed, m in sigs:
                channel_key = CHANNEL_FOR_TIER.get(dest)
                if not channel_key:
                    continue
                channel_id = channels.get(channel_key)
                if not channel_id:
                    continue
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except Exception:
                        continue
                priority = TIER_CFG[tier]["priority"]
                await queue_discord_send(channel, embed, channel_key, self.logger, priority=priority)

        # Telegram — send once (not per-server)
        for dest, tier, embed, m in sigs:
            tg_signal = TELEGRAM_SIGNAL.get(dest)
            if not tg_signal:
                continue
            targets = get_telegram_targets(tg_signal)
            if not targets:
                continue
            try:
                text = _embed_to_telegram(embed, m)
            except Exception:
                continue
            for target in targets:
                await queue_telegram_send(
                    target["chat_id"],
                    target["thread_id"],
                    text,
                    tg_signal,
                    self.logger,
                    delay_seconds=target.get("delay_seconds", 0),
                )

    # ── DAILY DIGEST ───────────────────────────────────────────────────────

    async def _send_digest(self, mks: Optional[List[dict]] = None):
        if not mks:
            mks = await self._fetch_all()

        now = datetime.now(timezone.utc)
        ds = now.strftime("%A, %d %B %Y")
        gold = 0xC8A84E

        hdr = discord.Embed(
            title=f"\U0001f4ca Daily Oracle — {ds}",
            colour=gold,
            timestamp=now,
            description=(f"**{len(self._today)} signals** fired today\n"
                         f"**{len(mks)}** active markets tracked"),
        )
        hdr.set_author(name="Shaolin Saga", icon_url=self.icon_url)
        hdr.set_footer(text="Powered by Shaolin Saga!", icon_url=self.icon_url)

        movers = sorted(
            [m for m in mks
             if (m.get("volume24hr") or 0) >= 10_000
             and abs(m.get("oneDayPriceChange") or 0) >= 0.05],
            key=lambda m: abs(m.get("oneDayPriceChange") or 0),
            reverse=True,
        )[:6]
        ml = "\n".join(
            f"{CU if (m.get('oneDayPriceChange') or 0) > 0 else CD} "
            f"{_sp(m.get('oneDayPriceChange') or 0):>7}  "
            f"{(m.get('question') or '')[:38]} → {_top(m)[1]} {_pct(_top(m)[0])}"
            for m in movers
        ) or "No big movers today."
        me = discord.Embed(title="Biggest Movers", description=f"```\n{ml}```", colour=gold)
        me.set_author(name="Shaolin Saga", icon_url=self.icon_url)

        topv = sorted(mks, key=lambda m: m.get("volume24hr") or 0, reverse=True)[:5]
        vl = "\n".join(
            f"{_usd(m.get('volume24hr', 0)):>6}  "
            f"{(m.get('question') or '')[:38]} → {_top(m)[1]} {_pct(_top(m)[0])}"
            for m in topv
        )
        ve = discord.Embed(
            title="\U0001f4b0 Highest Volume (24h)",
            description=f"```\n{vl}```",
            colour=gold,
        )
        ve.set_author(name="Shaolin Saga", icon_url=self.icon_url)

        embeds = [hdr, me, ve]

        res = sorted(
            [m for m in mks
             if 0 < _days_until(m.get("endDate") or m.get("endDateIso")) <= 7
             and _top(m)[0] >= 0.85
             and (m.get("liquidityNum") or 0) >= 2_000],
            key=lambda m: _days_until(m.get("endDate") or m.get("endDateIso")),
        )[:5]
        if res:
            rl = "\n".join(
                f"{_days_until(m.get('endDate') or m.get('endDateIso')):.1f}d  "
                f"{(m.get('question') or '')[:33]} → {_top(m)[1]} {_pct(_top(m)[0])}"
                for m in res
            )
            re_embed = discord.Embed(
                title="\u23f0 Resolving Soon",
                description=f"```\n{rl}```",
                colour=gold,
            )
            re_embed.set_author(name="Shaolin Saga", icon_url=self.icon_url)
            embeds.append(re_embed)

        crypto_movers = [
            m for m in movers
            if _is_crypto((m.get("question") or "").lower())
            or _macro_hit((m.get("question") or "").lower())
        ]
        if crypto_movers:
            cl = "\n".join(
                f"{CU if (m.get('oneDayPriceChange') or 0) > 0 else CD} "
                f"{_sp(m.get('oneDayPriceChange') or 0):>7}  "
                f"{(m.get('question') or '')[:45]}"
                for m in crypto_movers[:4]
            )
            ce = discord.Embed(
                title="\U0001f4e1 Crypto Macro",
                description=f"```\n{cl}```",
                colour=gold,
            )
            ce.set_author(name="Shaolin Saga", icon_url=self.icon_url)
            embeds.append(ce)

        embeds[-1].set_footer(text="Powered by Shaolin Saga!", icon_url=self.icon_url)

        # Post to each server's oracle_daily channel
        for server in self.servers.get("allowed_servers", []):
            channel_id = server.get("channels", {}).get("oracle_daily")
            if not channel_id:
                continue
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    continue
            for embed in embeds:
                await queue_discord_send(
                    channel, embed, "oracle_daily", self.logger,
                    priority=MessagePriority.LOW,
                )

        # Telegram daily digest
        tg_targets = get_telegram_targets("polymarket_daily")
        if tg_targets:
            try:
                text = format_polymarket_daily(movers, topv, res, len(self._today), len(mks))
                for target in tg_targets:
                    await queue_telegram_send(
                        target["chat_id"], target["thread_id"], text,
                        "polymarket_daily", self.logger,
                        delay_seconds=target.get("delay_seconds", 0),
                    )
            except Exception as e:
                self.logger.error("Polymarket daily Telegram error: %s", e)

    # ── MAIN LOOPS ─────────────────────────────────────────────────────────

    async def _scan_loop(self):
        while True:
            try:
                self._n += 1
                mks = await self._fetch_all()
                if mks:
                    for m in mks:
                        self._snap(m)

                    sigs = (
                        self._d_vsurge(mks)
                        + self._d_reversal(mks)
                        + self._d_macro(mks)
                        + self._d_consensus(mks)
                        + self._d_velocity(mks)
                        + self._d_early(mks)
                        + self._d_airdrop(mks)
                        + self._d_resolution(mks)
                        + self._d_contrarian(mks)
                        + self._d_convergence(mks)
                    )

                    for _, _, e, _ in sigs:
                        self._today.append(e)

                    if sigs:
                        await self._post(sigs)
                        self.logger.info("Polymarket scan #%d: %d signals fired", self._n, len(sigs))
                    else:
                        self.logger.debug("Polymarket scan #%d: no signals", self._n)

                    self._cleanup()
                    self._save_state()

            except Exception as e:
                self.logger.error("Polymarket scan error: %s", e, exc_info=True)

            await asyncio.sleep(self.scan_interval)

    async def _digest_loop(self):
        while True:
            try:
                now = datetime.now(timezone.utc)
                td = now.strftime("%Y-%m-%d")
                if now.hour == self.digest_hour_utc and self._last_digest != td:
                    await self._send_digest()
                    self._last_digest = td
                    self._today = []
                    self._save_state()
            except Exception as e:
                self.logger.error("Polymarket digest error: %s", e, exc_info=True)
            await asyncio.sleep(300)


# ── TELEGRAM PLAIN-TEXT RENDERER ─────────────────────────────────────────────

def _embed_to_telegram(embed: discord.Embed, m: dict) -> str:
    """
    Convert a Discord embed to a Telegram HTML string.
    Used as a fallback for alert/signal embeds that don't have a
    dedicated formatter. Dedicated formatters in telegram_formatter.py
    should be preferred for the daily digest.
    """
    slug = m.get("slug", "")
    url = f"https://polymarket.com/event/{slug}" if slug else ""
    title = embed.title or ""
    lines = [f"<b>{title}</b>"]
    if url:
        lines.append(f'<a href="{url}">View on Polymarket</a>')
    for field in embed.fields:
        name = field.name or ""
        value = (field.value or "").replace("```", "").strip()
        if name and value:
            lines.append(f"\n<b>{name}</b>\n{value}")
    return "\n".join(lines)
