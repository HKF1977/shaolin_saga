"""
Polymarket Main — Shaolin Saga
================================
Entry point for the Polymarket monitor. Called from pump_main.py on_ready
via bot.loop.create_task(polymarket_main.start_monitoring(bot, servers)).

No separate process or bot token needed — shares the pump_main bot instance
and rate limiter infrastructure.
"""

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from polymarket_monitor import PolymarketMonitor

sys.path.append('/home/shaolin_saga/config')
from config import SS_ICON_URL

# Module-level state
_monitor: PolymarketMonitor = None


def setup_logger() -> logging.Logger:
    logger = logging.getLogger('polymarket')
    logger.setLevel(logging.DEBUG)
    os.makedirs('/home/shaolin_saga/logs', exist_ok=True)
    fh = RotatingFileHandler(
        '/home/shaolin_saga/logs/polymarket_monitor.log',
        maxBytes=1024 * 1024 * 10,
        backupCount=5,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)
    return logger


polymarket_logger = setup_logger()


async def start_monitoring(bot_instance, servers_config):
    """
    Start the Polymarket monitor. Called from pump_main.py on_ready:

        bot.loop.create_task(polymarket_main.start_monitoring(bot, servers))
    """
    global _monitor

    polymarket_logger.info("Starting Polymarket monitor")
    os.makedirs("/home/shaolin_saga/data/polymarket", exist_ok=True)

    _monitor = PolymarketMonitor(
        bot=bot_instance,
        servers=servers_config,
        logger=polymarket_logger,
        icon_url=SS_ICON_URL,
        scan_interval=120,
        digest_hour_utc=21,
    )

    await _monitor.start()
    polymarket_logger.info("Polymarket monitor started — bot: %s", bot_instance.user)
