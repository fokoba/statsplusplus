"""StatsPlus API client package."""

from statsplusplus.client.statsplus import (
    configure,
    CookieExpiredError,
    TokenExpiredError,
    RateLimitedError,
)

__all__ = ["configure", "CookieExpiredError", "TokenExpiredError", "RateLimitedError"]
