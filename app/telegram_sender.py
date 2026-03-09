import asyncio
import time
import aiohttp
import os
import json
import logging
from collections import defaultdict, deque
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Load telegram channels config
with open('/home/shaolin_saga/config/telegram_channels.json', 'r') as f:
    _tg_config = json.load(f)


def get_telegram_targets(signal_type: str) -> list[dict]:
    """Return list of {chat_id, thread_id} dicts for a given signal type."""
    targets = []
    for group in _tg_config['groups']:
        thread_id = group['topics'].get(signal_type)
        if thread_id is not None:
            targets.append({
                'chat_id': group['chat_id'],
                'thread_id': thread_id
            })
    return targets


class TelegramRateLimiter:
    """Telegram limits: 30 msg/sec globally, 1 msg/sec per chat."""
    def __init__(self):
        self.global_requests = deque()
        self.chat_requests = defaultdict(deque)

    def _cleanup(self):
        now = time.time()
        while self.global_requests and self.global_requests[0] < now - 1:
            self.global_requests.popleft()
        for chat_id in list(self.chat_requests):
            while self.chat_requests[chat_id] and self.chat_requests[chat_id][0] < now - 1:
                self.chat_requests[chat_id].popleft()

    async def acquire(self, chat_id: str):
        while True:
            self._cleanup()
            if len(self.global_requests) < 28 and len(self.chat_requests[chat_id]) < 1:
                break
            await asyncio.sleep(0.1)
        now = time.time()
        self.global_requests.append(now)
        self.chat_requests[chat_id].append(now)


_rate_limiter = TelegramRateLimiter()
_queue = None


async def safe_telegram_send(chat_id: str, thread_id: int, text: str, signal_type: str = "unknown", logger=None) -> bool:
    """Send a message to a Telegram group topic with rate limiting."""
    if not TELEGRAM_BOT_TOKEN:
        if logger:
            logger.warning("TELEGRAM_BOT_TOKEN not set, skipping Telegram send")
        return False

    await _rate_limiter.acquire(chat_id)

    payload = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{BASE_URL}/sendMessage", json=payload) as resp:
                if resp.status == 200:
                    if logger:
                        logger.debug(f"Telegram: sent to {chat_id} thread {thread_id} ({signal_type})")
                    return True
                elif resp.status == 429:
                    data = await resp.json()
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    if logger:
                        logger.warning(f"Telegram rate limited on {signal_type}, retry in {retry_after}s")
                    await asyncio.sleep(retry_after)
                    return False
                else:
                    body = await resp.text()
                    if logger:
                        logger.error(f"Telegram send failed {resp.status}: {body}")
                    return False
    except Exception as e:
        if logger:
            logger.error(f"Telegram send error on {signal_type}: {e}")
        return False


class _TelegramMessage:
    def __init__(self, chat_id, thread_id, text, signal_type, logger):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.text = text
        self.signal_type = signal_type
        self.logger = logger


class TelegramQueue:
    def __init__(self, max_size=500):
        self.queue = asyncio.Queue(maxsize=max_size)
        self.is_processing = False
        self.dropped = defaultdict(int)

    async def add(self, chat_id, thread_id, text, signal_type, logger):
        msg = _TelegramMessage(chat_id, thread_id, text, signal_type, logger)
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.dropped[signal_type] += 1
            if logger:
                logger.warning(f"Telegram queue full, dropped {signal_type}")

    async def process(self):
        self.is_processing = True
        while True:
            try:
                try:
                    msg = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await safe_telegram_send(
                    msg.chat_id, msg.thread_id, msg.text, msg.signal_type, msg.logger
                )
            except Exception as e:
                print(f"Telegram queue error: {e}")
                await asyncio.sleep(5)


def get_queue() -> TelegramQueue:
    global _queue
    if _queue is None:
        _queue = TelegramQueue()
    return _queue


async def queue_telegram_send(chat_id: str, thread_id: int, text: str, signal_type: str = "unknown", logger=None):
    """Queue a Telegram message for sending."""
    await get_queue().add(chat_id, thread_id, text, signal_type, logger)


async def ensure_telegram_queue_processing():
    """Ensure the Telegram message queue is running."""
    q = get_queue()
    if not q.is_processing:
        asyncio.create_task(q.process())
