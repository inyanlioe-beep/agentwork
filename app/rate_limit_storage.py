# Rate limiting storage
from collections import deque

# Module-level rate limiting storage for fallback
request_windows: dict[str, deque[float]] = {}