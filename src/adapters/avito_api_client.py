"""
Avito Messenger API HTTP client.

Wraps all REST calls needed by the bot:
  - send_message   POST /messenger/v1/.../messages
  - get_chat       GET  /messenger/v2/.../chats/{chat_id}   (for sender name)
  - mark_read      POST /messenger/v1/.../read
  - get_self       GET  /core/v1/accounts/self              (to discover user_id)

Auth: Bearer token via AvitoAuthClient.
On HTTP 401 the token is invalidated and the request is retried exactly once.
The aiohttp.ClientSession is persistent (connection pooling); call start() before
use and close() on shutdown.
"""

import logging
from typing import Any, Optional

import aiohttp

from .avito_auth import AvitoAuthClient

logger = logging.getLogger(__name__)

_BASE = "https://api.avito.ru"
_MSG_LIMIT = 1000  # Avito hard limit on text message length


class AvitoApiClient:
    """
    Thin async wrapper around Avito Messenger REST API.

    Usage:
        client = AvitoApiClient(auth)
        await client.start()
        ...
        await client.close()
    """

    def __init__(self, auth: AvitoAuthClient) -> None:
        self._auth = auth
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(base_url=_BASE)
        logger.info("AvitoApiClient session opened")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("AvitoApiClient session closed")

    # ── Public methods ─────────────────────────────────────────────────────────

    async def send_message(self, user_id: int, chat_id: str, text: str) -> dict:
        """Send a text message. Avito truncates silently at 1000 chars — we truncate first."""
        if len(text) > _MSG_LIMIT:
            logger.warning("Message truncated from %d to %d chars", len(text), _MSG_LIMIT)
            text = text[:_MSG_LIMIT]
        return await self._request(
            "POST",
            f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
            json={"message": {"text": text}, "type": "text"},
        )

    async def get_chat(self, user_id: int, chat_id: str) -> dict:
        """Return chat object including users[] array with names."""
        return await self._request(
            "GET",
            f"/messenger/v2/accounts/{user_id}/chats/{chat_id}",
        )

    async def mark_read(self, user_id: int, chat_id: str) -> None:
        """Mark all messages in the chat as read (Avito requires this after reading)."""
        await self._request(
            "POST",
            f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/read",
        )

    async def get_webhook_subscriptions(self) -> list[dict]:
        """
        POST /messenger/v1/subscriptions — returns active webhook subscriptions.
        Note: Avito uses POST for this read operation (per their Swagger spec).
        Each item has 'url' and 'version' fields.
        """
        data = await self._request("POST", "/messenger/v1/subscriptions")
        return data.get("subscriptions", [])

    async def register_webhook(self, url: str) -> None:
        """POST /messenger/v3/webhook — register a webhook URL for message notifications."""
        await self._request("POST", "/messenger/v3/webhook", json={"url": url})

    async def get_self(self) -> dict:
        """
        GET /core/v1/accounts/self — returns seller profile including numeric 'id'.
        Use this once at startup to confirm AVITO_USER_ID is correct.
        """
        return await self._request("GET", "/core/v1/accounts/self")

    def extract_sender_name(self, chat: dict, sender_id: int) -> str:
        """
        Pull a display name for sender_id out of a chat object from get_chat().
        Falls back to the string representation of the id if not found.
        """
        for user in chat.get("users", []):
            if user.get("id") == sender_id:
                return user.get("name") or str(sender_id)
        return str(sender_id)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        _retry: bool = True,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("AvitoApiClient.start() must be called before use")

        token = await self._auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with self._session.request(
            method, path, json=json, headers=headers
        ) as resp:
            if resp.status == 401 and _retry:
                logger.warning("Avito 401 on %s %s — refreshing token and retrying", method, path)
                await self._auth.invalidate()
                return await self._request(method, path, json=json, _retry=False)

            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(
                    f"Avito API error: {method} {path} → HTTP {resp.status}: {body}"
                )

            # content_type=None: skip mime-type check (Avito sometimes sends text/plain)
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {}
