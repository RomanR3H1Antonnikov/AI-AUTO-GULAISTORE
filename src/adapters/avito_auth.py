"""
Avito OAuth2 token manager — client_credentials flow.

Used for accessing your own Avito seller account (not third-party users).
Token URL: POST https://api.avito.ru/token
Tokens expire in ~86400 s (24 h); we refresh 60 s before expiry.
On HTTP 401 from any API call, call invalidate() to force an immediate refresh.
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.avito.ru/token"
_REFRESH_BUFFER = 60  # seconds before expiry at which we proactively refresh


class AvitoAuthClient:
    """
    Caches and auto-refreshes an Avito access token.
    Concurrent callers share one refresh via asyncio.Lock (no thundering herd).
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[str] = None
        self._expires_at: float = 0.0  # monotonic clock
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a valid Bearer token, refreshing if needed."""
        if self._is_valid():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if self._is_valid():
                return self._token  # type: ignore[return-value]
            await self._do_refresh()
        return self._token  # type: ignore[return-value]

    async def invalidate(self) -> str:
        """Force a fresh token (call when an API returns 401)."""
        self._expires_at = 0.0
        return await self.get_token()

    def _is_valid(self) -> bool:
        return (
            self._token is not None
            and time.monotonic() < self._expires_at - _REFRESH_BUFFER
        )

    async def _do_refresh(self) -> None:
        logger.info("Requesting new Avito access token...")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Avito token endpoint returned {resp.status}: {body}"
                    )
                data = await resp.json()

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self._expires_at = time.monotonic() + expires_in
        logger.info("Avito token obtained, valid for %d s", expires_in)
