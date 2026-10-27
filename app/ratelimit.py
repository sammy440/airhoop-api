"""
Small in-memory rate limiter (sliding window per client IP and bucket).

Good enough for a single API instance; swap for Redis when scaling out. Behind a proxy, run uvicorn with --proxy-headers so
`request.client.host` is the real client address.
"""

import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, enabled: bool = True) -> None:
        self.clock = clock
        self.enabled = enabled
        self.hits: dict[tuple[str, str], deque[float]] = {}

    def check(self, bucket: str, key: str, limit: int, window_s: float) -> float | None:
        """Records a hit; returns seconds to wait if over the limit, else None."""
        if not self.enabled:
            return None
        now = self.clock()
        q = self.hits.setdefault((bucket, key), deque())
        while q and q[0] <= now - window_s:
            q.popleft()
        if len(q) >= limit:
            return q[0] + window_s - now
        q.append(now)
        if len(self.hits) > 50_000:  # keep memory bounded under abuse
            self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - window_s}
        return None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, limit: int, window_s: float):
    """FastAPI dependency: `Depends(rate_limit("guest", 10, 60))`."""

    def dependency(request: Request) -> None:
        limiter: RateLimiter = request.app.state.limiter
        wait = limiter.check(bucket, client_ip(request), limit, window_s)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                {"code": "rate_limited", "message": "Slow down a little and try again."},
                headers={"Retry-After": str(max(1, round(wait)))},
            )

    return dependency
