import time
import asyncio
import discord
import logging
from collections import defaultdict, deque
import json
import os
from enum import IntEnum
from typing import Optional, Callable, Any


class MessagePriority(IntEnum):
    LOW = 1      # Regular trades, all_tokens
    MEDIUM = 2   # Large trades, new tokens with socials
    HIGH = 3     # Whale trades, power creators
    CRITICAL = 4 # System alerts, health warnings

class PriorityMessage:
    def __init__(self, channel, embed, channel_type, logger, priority=MessagePriority.LOW):
        self.channel = channel
        self.embed = embed
        self.channel_type = channel_type
        self.logger = logger
        self.priority = priority
        self.timestamp = time.time()
    
    def __lt__(self, other):
        # Higher priority first, then older messages first
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.timestamp < other.timestamp

class DiscordRateLimitManager:
    def __init__(self):
        self.buckets = defaultdict(dict)
        self.global_reset = None
        self.global_requests = deque()  # Track all requests for global limit
        self.global_limit = 45  # Stay well under 50/sec limit
        self.request_queue = asyncio.Queue()
        self.is_processing = False
        self.banned_until = None
        self.consecutive_rate_limits = 0
        self._cleanup_task = None

    async def start_cleanup_task(self):
        """Start the automatic cleanup task"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def _periodic_cleanup(self):
        """Periodically clean up old requests"""
        while True:
            try:
                self.cleanup_old_requests()
                await asyncio.sleep(5)  # Clean up every 5 seconds
            except Exception as e:
                print(f"Error in cleanup task: {e}")
                await asyncio.sleep(10)
        
    def is_banned(self):
        """Check if we're currently banned"""
        if self.banned_until and time.time() < self.banned_until:
            return True
        return False
    
    def record_ban(self, duration=300):  # 5 minute default ban
        """Record that we've been banned"""
        self.banned_until = time.time() + duration
        self.consecutive_rate_limits += 1
        print(f"🚫 BANNED for {duration}s. Consecutive rate limits: {self.consecutive_rate_limits}")
    
    def cleanup_old_requests(self):
        """Remove requests older than 1 second"""
        now = time.time()
        while self.global_requests and self.global_requests[0] < now - 1:
            self.global_requests.popleft()
    
    def can_make_request(self):
        """Check if we can make a request without hitting global limit"""
        if self.is_banned():
            return False
            
        self.cleanup_old_requests()
        return len(self.global_requests) < self.global_limit
    
    async def wait_for_global_limit(self):
        """Wait until we can make a request"""
        while not self.can_make_request():
            if self.is_banned():
                wait_time = self.banned_until - time.time()
                print(f"⏳ Waiting for ban to lift: {wait_time:.1f}s")
                await asyncio.sleep(min(wait_time, 10))
            else:
                # Wait for oldest request to expire
                await asyncio.sleep(0.1)
    
    def record_request(self):
        """Record that we made a request"""
        self.global_requests.append(time.time())
    
    async def wait_if_needed(self, bucket_id=None):
        """Wait if we're rate limited"""
        current_time = time.time()
        
        # Check if we're banned first
        if self.is_banned():
            wait_time = self.banned_until - current_time
            print(f"🚫 Currently banned, waiting {wait_time:.1f} seconds")
            await asyncio.sleep(wait_time)
            return
        
        # Check global rate limit
        await self.wait_for_global_limit()
        
        # Check bucket-specific rate limit
        if bucket_id and bucket_id in self.buckets:
            bucket = self.buckets[bucket_id]
            if bucket.get('remaining', 1) <= 0 and current_time < bucket.get('reset_time', 0):
                wait_time = bucket['reset_time'] - current_time
                print(f"⏳ Bucket {bucket_id} rate limited, waiting {wait_time:.1f} seconds")
                await asyncio.sleep(wait_time)
    
    def update_from_response(self, response_headers, bucket_id=None):
        """Update rate limit info from Discord response headers"""
        try:
            limit = response_headers.get('X-RateLimit-Limit')
            remaining = response_headers.get('X-RateLimit-Remaining')
            reset_after = response_headers.get('X-RateLimit-Reset-After')
            bucket = response_headers.get('X-RateLimit-Bucket')
            is_global = response_headers.get('X-RateLimit-Global')
            scope = response_headers.get('X-RateLimit-Scope')
            
            if is_global or scope == 'global':
                reset_time = float(reset_after) if reset_after else 60
                self.global_reset = time.time() + reset_time
                print(f"🚫 Global rate limit hit! Reset in {reset_after} seconds")
                self.record_ban(reset_time + 10)  # Add buffer
            
            if bucket and limit and remaining and reset_after:
                self.buckets[bucket] = {
                    'limit': int(limit),
                    'remaining': int(remaining),
                    'reset_after': float(reset_after),
                    'reset_time': time.time() + float(reset_after)
                }
                
        except Exception as e:
            print(f"Error parsing rate limit headers: {e}")


async def safe_discord_send(channel, embed, channel_type="unknown", logger=None):
    """Send Discord message with aggressive rate limit handling"""
    max_retries = 2  # Reduce retries
    base_delay = 10   # Increase base delay
    
    for attempt in range(max_retries):
        try:
            # Wait for rate limits BEFORE attempting
            await rate_limiter.wait_if_needed()
            
            # Record that we're making a request
            rate_limiter.record_request()
            
            # Send the message
            message = await channel.send(embed=embed)
            
            # Reset consecutive rate limits on success
            rate_limiter.consecutive_rate_limits = 0
            
            if logger:
                logger.debug(f"✅ Sent to {channel} for {channel_type} successfully")
            return message
            
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = getattr(e, 'retry_after', base_delay)
                
                # If we're getting rate limited frequently, increase delays
                if rate_limiter.consecutive_rate_limits > 3:
                    retry_after *= 2
                
                if logger:
                    logger.warning(f"🚫 Rate limited on {channel_type}, waiting {retry_after}s (attempt {attempt + 1})")
                
                # Update rate limiter with response info
                if hasattr(e, 'response') and hasattr(e.response, 'headers'):
                    rate_limiter.update_from_response(e.response.headers)
                
                # Check if this is a global rate limit or ban
                error_text = str(e).lower()
                if 'blocked' in error_text or 'temporarily' in error_text:
                    # We're banned - wait much longer
                    ban_duration = 600  # 10 minutes
                    rate_limiter.record_ban(ban_duration)
                    if logger:
                        logger.critical(f"🚫 DISCORD BAN DETECTED! Waiting {ban_duration}s")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(ban_duration)
                        continue
                    else:
                        raise
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    if logger:
                        logger.error(f"❌ Failed to send to {channel_type} after {max_retries} attempts")
                    # Don't raise - just drop the message to prevent cascade failures
                    return None
            else:
                if logger:
                    logger.error(f"❌ Discord error on {channel_type}: {e}")
                return None
                
        except Exception as e:
            if logger:
                logger.error(f"❌ Unexpected error sending to {channel_type}: {e}")
            return None

'''
# Add message queuing for high-volume periods
class MessageQueue:
    def __init__(self, max_queue_size=500):  # Increased from 100
        self.queue = asyncio.PriorityQueue(maxsize=max_queue_size)
        self.is_processing = False
        self.dropped_messages = defaultdict(int)
        
    async def add_message(self, channel, embed, channel_type, logger, priority=MessagePriority.LOW):
        """Add message to queue with priority"""
        message = PriorityMessage(channel, embed, channel_type, logger, priority)
        
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop lower priority messages first
            if priority >= MessagePriority.HIGH:
                # For high priority, try to make room by dropping old low priority
                await self._make_room_for_priority(message)
            else:
                self.dropped_messages[channel_type] += 1
                if logger and self.dropped_messages[channel_type] % 10 == 1:  # Log every 10th drop
                    logger.warning(f"⚠️ Message queue full, dropped {self.dropped_messages[channel_type]} {channel_type} messages")
    
    async def _make_room_for_priority(self, high_priority_message):
        """Try to make room for high priority messages"""
        temp_messages = []
        made_room = False
        
        # Try to remove up to 5 low priority messages
        for _ in range(5):
            try:
                message = self.queue.get_nowait()
                if message.priority <= MessagePriority.LOW:
                    # Drop this low priority message
                    made_room = True
                    break
                else:
                    temp_messages.append(message)
            except asyncio.QueueEmpty:
                break
        
        # Put back the messages we didn't drop
        for msg in temp_messages:
            try:
                self.queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # If we can't put it back, drop it
        
        # Try to add our high priority message
        if made_room:
            try:
                self.queue.put_nowait(high_priority_message)
            except asyncio.QueueFull:
                pass
    
    async def process_queue(self):
        """Process queued messages with adaptive timing"""
        self.is_processing = True
        consecutive_successes = 0
        
        while True:
            try:
                # Get message from queue
                try:
                    message = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Send with rate limiting
                result = await safe_discord_send(
                    message.channel, 
                    message.embed, 
                    message.channel_type, 
                    message.logger
                )
                
                if result is not None:
                    consecutive_successes += 1
                else:
                    consecutive_successes = 0
                
                # Adaptive delay based on success rate and queue size
                queue_size = self.queue.qsize()
                
                if queue_size > 400:  # Queue very full
                    delay = 0.1  # Send faster
                elif queue_size > 200:  # Queue moderately full
                    delay = 0.2
                elif queue_size > 50: 
                    delay = 0.5
                elif consecutive_successes > 10:  # We're doing well
                    delay = 1.0
                else:
                    delay = 1.5  # Default safe delay
                
                await asyncio.sleep(delay)
                
            except Exception as e:
                print(f"Error processing message queue: {e}")
                await asyncio.sleep(5)
'''

# Replace the entire MessageQueue class with this:
class MessageQueue:
    def __init__(self, max_queue_size=1000):  # INCREASED SIZE
        self.queue = asyncio.PriorityQueue(maxsize=max_queue_size)
        self.is_processing = False
        self.dropped_messages = defaultdict(int)
        self.processed_count = 0
        self.last_stats_log = time.time()
    
    async def add_message(self, channel, embed, channel_type, logger, priority=MessagePriority.LOW):
        """Add message to queue with priority"""
        message = PriorityMessage(channel, embed, channel_type, logger, priority)
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop lower priority messages first
            if priority >= MessagePriority.HIGH:
                # For high priority, try to make room by dropping old low priority
                await self._make_room_for_priority(message)
            else:
                self.dropped_messages[channel_type] += 1
                if logger and self.dropped_messages[channel_type] % 10 == 1:  # Log every 10th drop
                    logger.warning(f"📬 Message queue full, dropped {self.dropped_messages[channel_type]} {channel_type} messages")

    async def _make_room_for_priority(self, priority_message):
        """Try to make room for high priority message by dropping low priority ones"""
        temp_messages = []
        made_room = False
        
        # Try to find and remove a low priority message
        try:
            while not self.queue.empty():
                message = self.queue.get_nowait()
                if message.priority < MessagePriority.HIGH and not made_room:
                    # Drop this low priority message
                    self.dropped_messages[message.channel_type] += 1
                    made_room = True
                else:
                    temp_messages.append(message)
            
            # Put back the messages we want to keep
            for msg in temp_messages:
                self.queue.put_nowait(msg)
            
            # Add the high priority message if we made room
            if made_room:
                self.queue.put_nowait(priority_message)
            else:
                # Still couldn't make room, drop the high priority message too
                self.dropped_messages[priority_message.channel_type] += 1
                
        except Exception as e:
            # If something goes wrong, put messages back
            for msg in temp_messages:
                try:
                    self.queue.put_nowait(msg)
                except:
                    pass

    async def process_queue(self):
        """Process queued messages with better monitoring"""
        self.is_processing = True
        consecutive_successes = 0
        
        while True:
            try:
                # Log stats every 60 seconds
                if time.time() - self.last_stats_log > 60:
                    await self._log_queue_stats()
                    self.last_stats_log = time.time()
                
                # Get message from queue
                try:
                    message = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Send with rate limiting
                result = await safe_discord_send(
                    message.channel, 
                    message.embed, 
                    message.channel_type, 
                    message.logger
                )
                
                if result is not None:
                    consecutive_successes += 1
                    self.processed_count += 1
                else:
                    consecutive_successes = 0
                
                # Adaptive delay based on success rate and queue size
                queue_size = self.queue.qsize()
                
                if queue_size > 800:  # Queue very full
                    delay = 0.05  # Send faster
                elif queue_size > 400:  # Queue moderately full
                    delay = 0.1
                elif queue_size > 100: 
                    delay = 0.2
                elif consecutive_successes > 10:  # We're doing well
                    delay = 0.5
                else:
                    delay = 1.0  # Default safe delay
                
                await asyncio.sleep(delay)
                
            except Exception as e:
                print(f"Error processing message queue: {e}")
                await asyncio.sleep(5)
    
    async def _log_queue_stats(self):
        """Log queue statistics"""
        queue_size = self.queue.qsize()
        total_dropped = sum(self.dropped_messages.values())
        print(f"📊 Queue Stats: Size={queue_size}, Processed={self.processed_count}, Dropped={total_dropped}")
        
        # Reset counters periodically
        if self.processed_count > 10000:
            self.processed_count = 0
            self.dropped_messages.clear()

class EmergencyBrake:
    def __init__(self):
        self.rate_limit_count = 0
        self.last_reset = time.time()
        self.is_emergency_mode = False
        
    def record_rate_limit(self):
        """Record a rate limit hit"""
        now = time.time()
        
        # Reset counter every hour
        if now - self.last_reset > 3600:
            self.rate_limit_count = 0
            self.last_reset = now
            self.is_emergency_mode = False
        
        self.rate_limit_count += 1
        
        # If we hit more than 10 rate limits in an hour, enable emergency mode
        if self.rate_limit_count > 10:
            self.is_emergency_mode = True
            print(f"🚨 EMERGENCY MODE ACTIVATED - {self.rate_limit_count} rate limits in last hour")
    
    def should_send_message(self):
        """Check if we should send a message based on emergency mode"""
        if self.is_emergency_mode:
            # In emergency mode, only send 1 in every 10 messages
            return time.time() % 10 < 1
        return True

# Global instances
emergency_brake = EmergencyBrake()
_message_queue = None

def get_message_queue():
    """Get or create the message queue in the current event loop"""
    global _message_queue
    if _message_queue is None:
        _message_queue = MessageQueue()
    return _message_queue

async def queue_discord_send(channel, embed, channel_type="unknown", logger=None, priority=MessagePriority.LOW):
    """Queue a Discord message instead of sending immediately"""
    queue = get_message_queue()
    await queue.add_message(channel, embed, channel_type, logger, priority)

async def monitor_rate_limits(logger):
    """Monitor and log rate limit status"""
    while True:
        try:
            # Clean up old requests
            rate_limiter.cleanup_old_requests()
            
            # Get current status
            current_rate = len(rate_limiter.global_requests)
            is_banned = rate_limiter.is_banned()
            
            # Monitor queue status
            if _message_queue:
                queue_size = _message_queue.queue.qsize()
                if queue_size > 300:
                    logger.warning(f"📬 Message queue getting full: {queue_size}/500")
                elif queue_size > 100:
                    logger.info(f"📬 Message queue size: {queue_size}/500")
                
                # Log dropped message stats
                if _message_queue.dropped_messages:
                    total_dropped = sum(_message_queue.dropped_messages.values())
                    if total_dropped > 0:
                        logger.info(f"📉 Dropped messages: {dict(_message_queue.dropped_messages)} (total: {total_dropped})")
            
            # Log status every 30 seconds
            if int(time.time()) % 30 == 0:
                status = "🚫 BANNED" if is_banned else f"✅ OK ({current_rate}/45 req/s)"
                logger.info(f"📊 Rate Limit Status: {status}")
                
                if rate_limiter.consecutive_rate_limits > 0:
                    logger.warning(f"⚠️ Consecutive rate limits: {rate_limiter.consecutive_rate_limits}")
            
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Error in rate limit monitor: {e}")
            await asyncio.sleep(5)


# Global rate limiter instances
#rate_limiter = DiscordRateLimitManager()
#rpc_rate_limiter = RPCRateLimiter(max_requests_per_second=45)
'''
class RPCRateLimiter:
    def __init__(self, max_requests_per_second: int = 45, logger: Optional[logging.Logger] = None):
        """
        RPC Rate limiter to stay under Solana's 50 RPS limit
        Using 45 RPS to leave some buffer
        """
        self.max_rps = max_requests_per_second
        self.logger = logger or logging.getLogger(__name__)
        self.request_times = deque()
        self.lock = asyncio.Lock()
        
        # Statistics
        self.total_requests = 0
        self.rate_limited_requests = 0
        self.last_429_time = None
        self.consecutive_429s = 0
        
    async def acquire(self):
        """Acquire permission to make an RPC request"""
        async with self.lock:
            now = time.time()
            
            # Remove requests older than 1 second
            while self.request_times and self.request_times[0] < now - 1.0:
                self.request_times.popleft()
            
            # Check if we need to wait
            if len(self.request_times) >= self.max_rps:
                # Calculate how long to wait
                oldest_request = self.request_times[0]
                wait_time = 1.0 - (now - oldest_request)
                
                if wait_time > 0:
                    self.rate_limited_requests += 1
                    if self.logger:
                        self.logger.debug(f"RPC Rate limiting: waiting {wait_time:.3f}s (queue: {len(self.request_times)})")
                    await asyncio.sleep(wait_time)
                    now = time.time()
'''           
class RPCRateLimiter:
    def __init__(self, max_requests_per_second: int = 45, logger = None):
        """
        RPC Rate limiter to stay under Solana's 50 RPS limit
        Using 45 RPS to leave some buffer
        """
        self.max_rps = max_requests_per_second
        self.logger = logger or logging.getLogger(__name__)
        self.request_times = deque()
        self.lock = None  # Don't create the lock here!
        
        # Statistics
        self.total_requests = 0
        self.rate_limited_requests = 0
        self.last_429_time = None
        self.consecutive_429s = 0
    
    def _ensure_lock(self):
        """Create the lock in the correct event loop context"""
        if self.lock is None:
            try:
                self.lock = asyncio.Lock()
            except RuntimeError:
                # No event loop running, this will be created later
                pass
    
    async def acquire(self):
        """Acquire permission to make an RPC request"""
        # Ensure we have a lock in the current event loop
        if self.lock is None:
            self.lock = asyncio.Lock()
        
        async with self.lock:
            now = time.time()
            
            # Remove requests older than 1 second
            while self.request_times and self.request_times[0] < now - 1.0:
                self.request_times.popleft()
            
            # Check if we need to wait
            if len(self.request_times) >= self.max_rps:
                # Calculate how long to wait
                oldest_request = self.request_times[0]
                wait_time = 1.0 - (now - oldest_request)
                
                if wait_time > 0:
                    self.rate_limited_requests += 1
                    if self.logger:
                        self.logger.debug(f"RPC Rate limiting: waiting {wait_time:.3f}s (queue: {len(self.request_times)})")
                    await asyncio.sleep(wait_time)
                    now = time.time()
            
            # Record this request
            self.request_times.append(now)
            self.total_requests += 1
    
    
    def record_429(self):
        """Record that we received a 429 response"""
        self.consecutive_429s += 1
        self.last_429_time = time.time()
        
        # Exponential backoff for consecutive 429s
        if self.consecutive_429s > 1:
            backoff_time = min(2 ** (self.consecutive_429s - 1), 30)  # Max 30 seconds
            if self.logger:
                self.logger.warning(f"🚫 RPC Consecutive 429 #{self.consecutive_429s}, backing off for {backoff_time}s")
            return backoff_time
        
        return 1  # Base backoff of 1 second
    
    def record_success(self):
        """Record a successful request (resets 429 counter)"""
        if self.consecutive_429s > 0:
            if self.logger:
                self.logger.info(f"✅ RPC Request succeeded after {self.consecutive_429s} consecutive 429s")
            self.consecutive_429s = 0
    
    def get_stats(self):
        """Get rate limiter statistics"""
        return {
            'total_requests': self.total_requests,
            'rate_limited_requests': self.rate_limited_requests,
            'current_queue_size': len(self.request_times),
            'consecutive_429s': self.consecutive_429s,
            'last_429_time': self.last_429_time
        }

'''
# Add RPC-specific helper function
async def safe_rpc_call(rpc_func, *args, max_retries: int = 3, logger=None, **kwargs):
    """
    Safely make an RPC call with rate limiting and 429 handling
    
    Args:
        rpc_func: The RPC function to call
        *args: Arguments for the RPC function
        max_retries: Maximum number of retry attempts
        logger: Logger instance for debugging
        **kwargs: Keyword arguments for the RPC function
    """
    # Get rate limiter instance
    limiter = get_rpc_rate_limiter()
    
    # Handle case where rpc_rate_limiter failed to initialize
    if limiter is None:
        if logger:
            logger.warning("RPC rate limiter not available, making direct call")
        return await rpc_func(*args, **kwargs)
    
    for attempt in range(max_retries):
        try:
            # Rate limit before making request
            await limiter.acquire()
            
            if logger:
                logger.debug(f"Making RPC call: {rpc_func.__name__} (attempt {attempt + 1})")

            # Make the actual RPC call
            response = await rpc_func(*args, **kwargs)

            # Record successful request
            limiter.record_success()
            
            return response

        except Exception as e:
            # TEMPORARY: Log ALL errors to see what 429s look like
            if logger:
                logger.error(f"🔍 RPC ERROR DEBUG - Type: {type(e).__name__}")
                logger.error(f"🔍 RPC ERROR DEBUG - String: {str(e)}")
                logger.error(f"🔍 RPC ERROR DEBUG - Repr: {repr(e)}")

                # If it has attributes, log those too
                if hasattr(e, 'args'):
                    logger.error(f"🔍 RPC ERROR DEBUG - Args: {e.args}")
                if hasattr(e, 'response'):
                    logger.error(f"🔍 RPC ERROR DEBUG - Response: {e.response}")
                if hasattr(e, 'status_code'):
                    logger.error(f"🔍 RPC ERROR DEBUG - Status Code: {e.status_code}")


        #except Exception as e:
        #    # Comprehensive error logging
        #    error_str = str(e).lower()
            
            # Check for rate limiting errors
            if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
                if logger:
                    logger.error(f"🚫 RPC RATE LIMITED: {str(e)}")
                
                # Record 429 and get backoff time
                backoff_time = rpc_rate_limiter.record_429()
                
                # Extract wait time from error message if available
                wait_match = re.search(r'(\d+(?:\.\d+)?)\s*(ms|s)', error_str)
                if wait_match:
                    wait_value = float(wait_match.group(1))
                    unit = wait_match.group(2)
                    if unit == 'ms':
                        extracted_wait = wait_value / 1000
                    else:
                        extracted_wait = wait_value
                    
                    # Use the longer of extracted wait time or our backoff
                    backoff_time = max(backoff_time, extracted_wait)
                
                if logger:
                    logger.warning(f"⏳ RPC Backing off for {backoff_time:.3f} seconds")
                await asyncio.sleep(backoff_time)
                
            else:
                if logger:
                    logger.error(f"❌ RPC Exception: {type(e).__name__}: {str(e)}")

            # Standard retry logic
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                if logger:
                    logger.info(f"⏳ Retrying RPC call in {wait_time} seconds... (attempt {attempt + 2}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            else:
                if logger:
                    logger.error(f"💥 RPC call failed after {max_retries} attempts")
                raise
'''
'''
async def safe_rpc_call(rpc_func, *args, max_retries: int = 3, logger=None, **kwargs):
    """Safely make an RPC call with rate limiting and 429 handling"""

    # Get rate limiter instance
    limiter = get_rpc_rate_limiter()

    if limiter is None:
        if logger:
            logger.warning("RPC rate limiter not available, making direct call")
        return await rpc_func(*args, **kwargs)

    for attempt in range(max_retries):
        try:
            # Rate limit before making request
            await limiter.acquire()

            if logger:
                logger.debug(f"Making RPC call: {rpc_func.__name__} (attempt {attempt + 1})")

            # Make the actual RPC call
            response = await rpc_func(*args, **kwargs)

            # Record successful request
            limiter.record_success()

            return response

        except Exception as e:
            # Check if it's a SolanaRpcException and dig deeper
            if type(e).__name__ == 'SolanaRpcException':
                if logger:
                    logger.error(f"🔍 SOLANA RPC EXCEPTION DEBUG:")
                    logger.error(f"🔍 Dir: {[attr for attr in dir(e) if not attr.startswith('_')]}")

                    # Check common attributes
                    for attr in ['code', 'message', 'data', 'error', 'status', 'response', 'details']:
                        if hasattr(e, attr):
                            value = getattr(e, attr)
                            logger.error(f"🔍 {attr}: {value}")

                    # Try to get the original exception if it's wrapped
                    if hasattr(e, '__cause__'):
                        logger.error(f"🔍 Cause: {e.__cause__}")
                    if hasattr(e, '__context__'):
                        logger.error(f"🔍 Context: {e.__context__}")
            else:
                # Log other exception types normally
                if logger:
                    logger.error(f"🔍 OTHER ERROR - Type: {type(e).__name__}, String: {str(e)}")

            # For now, just re-raise to see what happens
            raise

    # This shouldn't be reached, but just in case
    raise Exception("Max retries exceeded")

'''

async def safe_rpc_call(rpc_func, *args, max_retries: int = 3, logger=None, **kwargs):
    """Safely make an RPC call with rate limiting and 429 handling"""

    # Get rate limiter instance
    limiter = get_rpc_rate_limiter()

    if limiter is None:
        if logger:
            logger.warning("RPC rate limiter not available, making direct call")
        return await rpc_func(*args, **kwargs)

    for attempt in range(max_retries):
        try:
            # Rate limit before making request
            await limiter.acquire()

            if logger:
                logger.debug(f"Making RPC call: {rpc_func.__name__} (attempt {attempt + 1})")

            # Make the actual RPC call
            response = await rpc_func(*args, **kwargs)

            # Record successful request
            limiter.record_success()

            return response

        except Exception as e:
            # Check for 429 rate limiting in SolanaRpcException
            is_429 = False

            if type(e).__name__ == 'SolanaRpcException':
                # Check the cause for 429 errors
                if hasattr(e, '__cause__') and e.__cause__:
                    cause_str = str(e.__cause__).lower()
                    if "429" in cause_str or "too many requests" in cause_str:
                        is_429 = True

                # Also check error_msg attribute if it exists
                if hasattr(e, 'error_msg') and e.error_msg:
                    error_msg_str = str(e.error_msg).lower()
                    if "429" in error_msg_str or "too many requests" in error_msg_str:
                        is_429 = True

            # Handle 429 errors
            if is_429:
                if logger:
                    logger.warning(f"🚫 RPC 429 Rate Limited (attempt {attempt + 1})")

                # Record the 429 and get backoff time
                backoff_time = limiter.record_429()

                if attempt < max_retries - 1:
                    if logger:
                        logger.info(f"⏳ Backing off for {backoff_time}s before retry")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    if logger:
                        logger.error(f"❌ Max retries exceeded for RPC call after 429s")
                    raise

            # Check for other network/connection errors
            elif any(err in str(e).lower() for err in ["connection", "timeout", "network", "unreachable"]):
                if logger:
                    logger.warning(f"🌐 RPC Network error (attempt {attempt + 1}): {e}")

                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    if logger:
                        logger.info(f"⏳ Retrying in {wait_time}s due to network error")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    if logger:
                        logger.error(f"❌ Max retries exceeded for RPC call after network errors")
                    raise

            # For other errors, don't retry
            else:
                if logger:
                    logger.error(f"❌ RPC call failed with non-retryable error: {type(e).__name__}: {e}")
                raise

    # This shouldn't be reached, but just in case
    raise Exception("Max retries exceeded")


async def log_rpc_stats(logger):
    """Periodically log RPC rate limiter statistics"""
    while True:
        try:
            await asyncio.sleep(300)  # Log every 5 minutes
            
            limiter = get_rpc_rate_limiter()
            if limiter is None:
                logger.warning("RPC rate limiter not available for stats")
                continue

            stats = rpc_rate_limiter.get_stats()
            
            logger.info(
                f"📊 RPC Stats: "
                f"Total: {stats['total_requests']}, "
                f"Rate Limited: {stats['rate_limited_requests']}, "
                f"Queue: {stats['current_queue_size']}, "
                f"429s: {stats['consecutive_429s']}"
            )
            
            # Log warning if too many rate limits
            if stats['total_requests'] > 0 and stats['rate_limited_requests'] > stats['total_requests'] * 0.1:  # More than 10%
                logger.warning(f"⚠️ High RPC rate limiting: {stats['rate_limited_requests']}/{stats['total_requests']} requests were rate limited")
                
        except Exception as e:
            logger.error(f"Error logging RPC stats: {e}")

# Global instances - create after all classes are defined
rate_limiter = DiscordRateLimitManager()

# Create RPC rate limiter with proper initialization
_rpc_rate_limiter = None

def get_rpc_rate_limiter():
    """Get or create the RPC rate limiter instance"""
    global _rpc_rate_limiter
    if _rpc_rate_limiter is None:
        try:
            _rpc_rate_limiter = RPCRateLimiter(max_requests_per_second=45)
        except Exception as e:
            print(f"Error creating RPC rate limiter: {e}")
            _rpc_rate_limiter = None
    return _rpc_rate_limiter

async def monitor_memory_usage(logger):
    """Monitor memory usage and queue health"""
    import psutil
    import os

    process = psutil.Process(os.getpid())

    while True:
        try:
            # Get memory usage
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024

            # Get queue stats
            queue = get_message_queue()
            queue_size = queue.queue.qsize() if queue else 0

            # Get rate limiter stats
            requests_count = len(rate_limiter.global_requests)

            logger.info(f"💾 Memory: {memory_mb:.1f}MB, Queue: {queue_size}, Requests: {requests_count}")

            # Alert if memory is too high
            if memory_mb > 1000:  # 1GB
                logger.warning(f"⚠️ High memory usage: {memory_mb:.1f}MB")

            # Alert if queue is too full
            if queue_size > 800:
                logger.warning(f"⚠️ Queue nearly full: {queue_size}")

            await asyncio.sleep(60)  # Check every minute

        except Exception as e:
            logger.error(f"Error monitoring memory: {e}")
            await asyncio.sleep(60)


async def ensure_queue_processing():
    """Ensure message queue is processing"""
    queue = get_message_queue()
    if not queue.is_processing:
        print("🚀 Starting message queue processor")
        asyncio.create_task(queue.process_queue())
    
    # Also start rate limiter cleanup
    await rate_limiter.start_cleanup_task()

# For backward compatibility
rpc_rate_limiter = get_rpc_rate_limiter()


