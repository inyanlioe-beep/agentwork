import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import redis.asyncio as redis
from fastapi import Request, HTTPException, status
from dataclasses import dataclass

from app.config import get_settings
from app.rate_limit_storage import request_windows
from collections import deque

logger = logging.getLogger(__name__)

@dataclass
class RateLimitConfig:
    window_seconds: int
    max_requests: int
    key_prefix: str

class RedisRateLimiter:
    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self.settings = get_settings()
        self.rate_limit_config = RateLimitConfig(
            window_seconds=60,
            max_requests=self.settings.task_rate_limit_per_minute,
            key_prefix="rate_limit:"
        )

    async def initialize(self, settings=None):
        """Initialize Redis connection"""
        if settings is not None:
            self.settings = settings
            self.rate_limit_config = RateLimitConfig(
                window_seconds=60,
                max_requests=settings.task_rate_limit_per_minute,
                key_prefix="rate_limit:",
            )
            request_windows.clear()
        if self.redis_client is not None:
            await self.redis_client.aclose()
            self.redis_client = None
        if self.redis_client is None:
            # Use Redis URL from environment or default to localhost
            redis_url = self.settings.redis_url or "redis://localhost:6379/0"
            try:
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                # Test connection
                await self.redis_client.ping()
                logger.info("Redis connection established")
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
                # Fall back to in-memory for development
                self.redis_client = None

    async def get_client_ip(self, request: Request) -> str:
        """Get client IP address from request"""
        direct_ip = request.client.host if request.client else "unknown"
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for and direct_ip in self.settings.trusted_proxy_ips:
            ip = forwarded_for.split(",")[0].strip()
            logger.debug(f"Using X-Forwarded-For IP: {ip}")
            return ip
        logger.debug(f"Using client IP: {direct_ip}")
        return direct_ip

    async def check_rate_limit(self, request: Request, key_prefix: str = None) -> bool:
        """Check if request is within rate limit"""
        if self.redis_client is None:
            # Fall back to in-memory rate limiting for development
            logger.debug("Using in-memory rate limiting")
            return await self._check_in_memory_rate_limit(request, key_prefix)
        
        try:
            client_ip = await self.get_client_ip(request)
            
            # Create rate limit key: prefix:ip:timestamp_window
            now = datetime.now(timezone.utc)
            window_start = int(now.timestamp() // self.rate_limit_config.window_seconds * self.rate_limit_config.window_seconds)
            
            key = f"{key_prefix or self.rate_limit_config.key_prefix}{client_ip}:{window_start}"
            
            # Use Redis pipeline for atomic increment and TTL
            pipe = self.redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.rate_limit_config.window_seconds + 10)  # Extra 10 seconds for safety
            results = await pipe.execute()
            
            current_count = results[0]
            
            if current_count > self.rate_limit_config.max_requests:
                # Calculate retry after
                retry_after = self.rate_limit_config.window_seconds - (int(now.timestamp()) % self.rate_limit_config.window_seconds)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded",
                    headers={"Retry-After": str(retry_after)}
                )
                
            return True
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in Redis rate limiting: {e}")
            # Fall back to in-memory
            logger.debug("Falling back to in-memory rate limiting due to Redis error")
            return await self._check_in_memory_rate_limit(request, key_prefix)

    async def _check_in_memory_rate_limit(self, request: Request, key_prefix: str = None) -> bool:
        """Fallback in-memory rate limiting"""
        client_ip = await self.get_client_ip(request)
        now = asyncio.get_event_loop().time()
        key = f"{key_prefix or self.rate_limit_config.key_prefix}{client_ip}"
        window = request_windows.setdefault(key, deque())
        
        # Clean old entries
        while window and now - window[0] >= 60:
            window.popleft()
        
        logger.debug(f"In-memory rate limit check for {client_ip}: window length = {len(window)}, max_requests = {self.rate_limit_config.max_requests}, window contents = {list(window)}")
        
        if len(window) >= self.rate_limit_config.max_requests:
            retry_after = max(1, int(60 - (now - window[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)}
            )
        
        window.append(now)
        return True

# Global rate limiter instance
rate_limiter = RedisRateLimiter()