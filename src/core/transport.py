from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IncomingMessage:
    dialog_id: str        # Platform-specific conversation ID
    sender_id: str        # Platform-specific sender ID
    text: str
    is_owner_message: bool
    transport_name: str
    raw: Optional[dict] = field(default=None, repr=False)


class Transport(ABC):
    """
    Platform-agnostic interface for sending/receiving messages.
    Implement once per platform: TelegramTransport, AvitoTransport, etc.
    The DialogEngine only depends on this interface.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier used as DB transport column value, e.g. 'telegram'."""
        ...

    @abstractmethod
    async def send_message(self, dialog_id: str, text: str) -> None:
        """Send a reply to the buyer in the given dialog."""
        ...

    @abstractmethod
    async def send_owner_notification(self, text: str) -> Optional[int]:
        """Send an alert to the store owner. Returns platform message ID if available."""
        ...

    @abstractmethod
    def get_dialog_link(self, dialog_id: str) -> str:
        """Human-readable link to the dialog for owner notifications."""
        ...

    @abstractmethod
    async def get_sender_name(self, sender_id: str) -> str:
        """Best-effort display name for the sender (used in notifications)."""
        ...
