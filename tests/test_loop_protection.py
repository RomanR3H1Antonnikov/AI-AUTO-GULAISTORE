"""
Tests for loop protection.

Covers:
  - Bot never responds to its own messages (is_owner_message not the right gate here —
    the adapter sets this field correctly; we test via engine's is_owner_message=True path)
  - Owner messages don't generate responses
  - Silenced dialog stays silent (toxicity path)
  - Dialog status 'silenced' blocks all replies
"""

import pytest
from unittest.mock import AsyncMock

from tests.conftest import (
    FakeTransport,
    _bot_reply,
    _lead_response,
    _toxic_response,
    make_msg,
)


def _patch_all(engine, bot_text="OK", is_toxic=False):
    async def side_effect(**kwargs):
        if kwargs.get("response_format") == {"type": "json_object"}:
            msgs = kwargs.get("messages", [])
            content = msgs[0]["content"] if msgs else ""
            if "токсич" in content.lower():
                return _toxic_response(is_toxic)
            return _lead_response(False)
        return _bot_reply(bot_text)
    engine.client.chat.completions.create = AsyncMock(side_effect=side_effect)


# ── Owner messages are silently ignored ───────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_message_returns_none(engine, transport):
    _patch_all(engine)

    msg = make_msg("Привет, это владелец", dialog_id="chat_lp1", is_owner=True)
    result = await engine.process_message(transport, msg)

    assert result is None
    assert len(transport.sent) == 0


@pytest.mark.asyncio
async def test_owner_message_does_not_create_conversation_entry(engine, transport, db):
    """Owner messages should trigger takeover on an existing dialog, not create new buyer dialogs."""
    _patch_all(engine)

    # Create a buyer dialog first
    await engine.process_message(transport, make_msg("Купить хочу", dialog_id="chat_lp2"))

    # Owner "responds"
    await engine.process_message(transport, make_msg("Иду!", dialog_id="chat_lp2", is_owner=True))

    dialog = await db.get_dialog("test", "chat_lp2")
    assert dialog["status"] == "owner_takeover"

    # Buyer sends another message — bot silent
    result = await engine.process_message(transport, make_msg("Когда?", dialog_id="chat_lp2"))
    assert result is None


# ── Silenced dialog ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_toxicity_silences_dialog(engine, transport, db):
    _patch_all(engine, is_toxic=True)

    msg = make_msg("*ругательство*", dialog_id="chat_lp3")
    result = await engine.process_message(transport, msg)

    assert result is None

    dialog = await db.get_dialog("test", "chat_lp3")
    assert dialog["status"] == "silenced"


@pytest.mark.asyncio
async def test_silenced_dialog_restored_on_non_toxic(engine, transport, db):
    """First clean message after toxicity silencing auto-restores the dialog."""
    _patch_all(engine, is_toxic=True)
    await engine.process_message(transport, make_msg("*мат*", dialog_id="chat_lp4"))

    dialog = await db.get_dialog("test", "chat_lp4")
    assert dialog["status"] == "silenced"

    # Switch to non-toxic — first clean follow-up should restore and reply
    _patch_all(engine, bot_text="Добрый день!", is_toxic=False)

    result = await engine.process_message(
        transport, make_msg("Прости, хочу купить", dialog_id="chat_lp4")
    )
    assert result is not None, "Non-toxic follow-up should restore dialog and get a reply"

    dialog = await db.get_dialog("test", "chat_lp4")
    assert dialog["status"] == "bot_active"


@pytest.mark.asyncio
async def test_silenced_dialog_stays_silent_on_repeat_toxic(engine, transport, db):
    """Repeated toxic messages while silenced receive no reply and no extra notification."""
    _patch_all(engine, is_toxic=True)
    await engine.process_message(transport, make_msg("*мат1*", dialog_id="chat_lp4b"))

    notif_count = len(transport.owner_notifications)

    for _ in range(2):
        result = await engine.process_message(
            transport, make_msg("*мат2*", dialog_id="chat_lp4b")
        )
        assert result is None, "Repeated toxic message while silenced must stay silent"

    assert len(transport.owner_notifications) == notif_count, \
        "No extra notifications for repeat toxic while silenced"


@pytest.mark.asyncio
async def test_toxicity_notifies_owner(engine, transport):
    _patch_all(engine, is_toxic=True)

    await engine.process_message(transport, make_msg("*оскорбление*", dialog_id="chat_lp5"))

    assert len(transport.owner_notifications) == 1
    notification = transport.owner_notifications[0]
    assert "токсич" in notification.lower() or "замолч" in notification.lower()


# ── No response to own messages (adapter-level protection) ───────────────────

@pytest.mark.asyncio
async def test_bot_own_message_flag_is_blocked(engine, transport):
    """is_owner_message=True is the engine-level gate for any 'operator' sender."""
    _patch_all(engine)

    # Simulate a scenario where the adapter sets is_owner_message=True (its own msg or owner msg)
    self_msg = make_msg("Добрый день!", dialog_id="chat_lp6", is_owner=True)
    result = await engine.process_message(transport, self_msg)
    assert result is None
