"""
Tests for AvitoTransport — all HTTP and owner notifications mocked.
"""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.adapters.avito_adapter import AvitoTransport
from src.adapters.avito_api_client import AvitoApiClient


def _make_transport(
    avito_user_id: int = 99,
) -> tuple[AvitoTransport, MagicMock, AsyncMock]:
    api = MagicMock(spec=AvitoApiClient)
    api.send_message = AsyncMock(return_value={"id": "msg1"})
    api.mark_read = AsyncMock(return_value={"ok": True})

    owner_notifier = AsyncMock()
    transport = AvitoTransport(api=api, avito_user_id=avito_user_id, owner_notifier=owner_notifier)
    return transport, api, owner_notifier


# ── name ──────────────────────────────────────────────────────────────────────

def test_name():
    transport, _, _ = _make_transport()
    assert transport.name == "avito"


# ── send_message ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_calls_api_with_user_id():
    transport, api, _ = _make_transport(avito_user_id=99)

    await transport.send_message("chat_abc", "Добрый день!")

    api.send_message.assert_awaited_once_with(99, "chat_abc", "Добрый день!")


@pytest.mark.asyncio
async def test_send_message_calls_mark_read_after_send():
    transport, api, _ = _make_transport(avito_user_id=99)

    await transport.send_message("chat_abc", "Ответ")

    api.mark_read.assert_awaited_once_with(99, "chat_abc")


@pytest.mark.asyncio
async def test_send_message_mark_read_failure_does_not_raise():
    """mark_read error must not propagate — reply is already delivered."""
    transport, api, _ = _make_transport()
    api.mark_read = AsyncMock(side_effect=RuntimeError("network error"))

    # Should not raise even though mark_read fails
    await transport.send_message("chat_abc", "Ответ")

    api.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_uses_injected_user_id():
    """Different user IDs should propagate to API calls."""
    transport, api, _ = _make_transport(avito_user_id=777)

    await transport.send_message("chat_xyz", "Текст")

    assert api.send_message.call_args[0][0] == 777
    assert api.mark_read.call_args[0][0] == 777


# ── send_owner_notification ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_owner_notification_calls_notifier():
    transport, _, owner_notifier = _make_transport()

    await transport.send_owner_notification("🔔 Новый лид!")

    owner_notifier.assert_awaited_once_with("🔔 Новый лид!")


@pytest.mark.asyncio
async def test_send_owner_notification_multiple_calls():
    transport, _, owner_notifier = _make_transport()

    await transport.send_owner_notification("Сообщение 1")
    await transport.send_owner_notification("Сообщение 2")

    assert owner_notifier.await_count == 2
    owner_notifier.assert_has_awaits([call("Сообщение 1"), call("Сообщение 2")])


# ── get_dialog_link ───────────────────────────────────────────────────────────

def test_get_dialog_link_format():
    transport, _, _ = _make_transport()
    link = transport.get_dialog_link("abc123")
    assert link == "https://www.avito.ru/profile/messenger/abc123"


def test_get_dialog_link_different_ids():
    transport, _, _ = _make_transport()
    assert transport.get_dialog_link("xyz999") == "https://www.avito.ru/profile/messenger/xyz999"


# ── get_sender_name ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_sender_name_returns_sender_id():
    """Fallback: return sender_id as-is (API requires chat_id which is unavailable here)."""
    transport, _, _ = _make_transport()
    name = await transport.get_sender_name("42")
    assert name == "42"


# ── Transport ABC compliance ──────────────────────────────────────────────────

def test_is_transport_subclass():
    from src.core.transport import Transport
    transport, _, _ = _make_transport()
    assert isinstance(transport, Transport)
