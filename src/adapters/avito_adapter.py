"""
Avito transport — Phase 2 full implementation.

AvitoTransport implements the Transport ABC using AvitoApiClient for all
outbound HTTP calls. Owner notifications are delegated to an injected
async callable (owner_notifier), which in the dual-mode setup is wired to
TelegramTransport.send_owner_notification so the store owner receives Avito
alerts in the same Telegram chat as Telegram-dialog alerts.

Why owner_notifier as a callable rather than a Transport reference:
  - Avoids circular imports (Telegram adapter would import Avito adapter)
  - Makes unit-testing trivial (pass any AsyncMock)
  - Keeps the coupling minimal

mark_read:
  After sending a reply we call mark_read() so the buyer sees a "read" receipt
  and the chat doesn't show as unread in our seller account. Failure is logged
  but does NOT abort the reply — the message is already delivered.

get_sender_name:
  DialogEngine does not call this in the current implementation. Avito's API
  requires chat_id (unavailable at this call site) to resolve a name. Returns
  sender_id as-is; improve later if the interface evolves.
"""

import logging
import time
from typing import Awaitable, Callable

from ..core.transport import Transport
from .avito_api_client import AvitoApiClient

logger = logging.getLogger(__name__)

OwnerNotifier = Callable[[str], Awaitable[None]]

# After the bot sends a message, Avito fires a webhook with author_id == user_id,
# indistinguishable from the seller typing manually. We track the send timestamp
# and ignore any owner webhook that arrives within this window.
_BOT_ECHO_WINDOW = 30  # seconds


class AvitoTransport(Transport):
    """
    Production Avito transport.

    Args:
        api:            Initialised AvitoApiClient (start() already called).
        avito_user_id:  Numeric seller account ID (path param for all API calls).
        owner_notifier: Async callable — sends a string to the store owner.
    """

    def __init__(
        self,
        api: AvitoApiClient,
        avito_user_id: int,
        owner_notifier: OwnerNotifier,
    ) -> None:
        self._api = api
        self._user_id = avito_user_id
        self._owner_notifier = owner_notifier
        self._last_sent: dict[str, float] = {}  # chat_id → monotonic timestamp

    # ── Transport interface ────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "avito"

    async def send_message(self, dialog_id: str, text: str) -> None:
        """Send reply to buyer, record timestamp for echo suppression, mark read."""
        await self._api.send_message(self._user_id, dialog_id, text)
        self._last_sent[dialog_id] = time.monotonic()
        try:
            await self._api.mark_read(self._user_id, dialog_id)
        except Exception:
            logger.warning("mark_read failed for chat %s (reply was sent)", dialog_id)

    def is_bot_echo(self, chat_id: str) -> bool:
        """True if the bot sent to this chat within the echo-suppression window."""
        sent_at = self._last_sent.get(chat_id)
        return sent_at is not None and (time.monotonic() - sent_at) < _BOT_ECHO_WINDOW

    async def send_owner_notification(self, text: str) -> None:
        """Forward alert to store owner via the injected notifier."""
        await self._owner_notifier(text)

    def get_dialog_link(self, dialog_id: str) -> str:
        return f"https://www.avito.ru/profile/messenger/{dialog_id}"

    async def get_sender_name(self, sender_id: str) -> str:
        return sender_id
