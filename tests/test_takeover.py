"""
Tests for the owner takeover mechanism.

Covers:
  - Auto-takeover when owner message arrives while bot is active
  - Bot stays silent in owner_takeover status
  - Manual takeover via handle_takeover()
  - Resuming bot via handle_resume()
  - Takeover logged correctly (type: auto vs manual)
"""

import pytest
from unittest.mock import AsyncMock, patch

from src.core.dialog_engine import DialogEngine
from tests.conftest import (
    FakeTransport,
    _bot_reply,
    _lead_response,
    _toxic_response,
    make_msg,
)


def _patch_llm(engine: DialogEngine, bot_text: str = "Добрый день!"):
    """Make all LLM calls return predictable responses."""
    async def side_effect(**kwargs):
        msgs = kwargs.get("messages", [])
        # Toxicity call (json_object mode)
        if kwargs.get("response_format") == {"type": "json_object"}:
            return _toxic_response(False)
        # Main LLM call
        return _bot_reply(bot_text)

    engine.client.chat.completions.create = AsyncMock(side_effect=side_effect)


# ── Auto-takeover ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_takeover_on_owner_message(engine, transport, db):
    """When the owner sends a message, the dialog should switch to owner_takeover."""
    _patch_llm(engine)

    # First, create a dialog by having a buyer send a message
    buyer_msg = make_msg("Привет, есть MacBook?", dialog_id="chat_42")
    await engine.process_message(transport, buyer_msg)

    dialog = await db.get_dialog("test", "chat_42")
    assert dialog["status"] == "bot_active"

    # Now simulate owner responding (e.g., in Avito the owner replies)
    owner_msg = make_msg("Да, приходите!", dialog_id="chat_42", is_owner=True)
    result = await engine.process_message(transport, owner_msg)

    assert result is None, "Bot must not respond to owner messages"

    dialog = await db.get_dialog("test", "chat_42")
    assert dialog["status"] == "owner_takeover"
    assert dialog["takeover_type"] == "auto"


# ── Bot silent in owner_takeover ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bot_silent_after_takeover(engine, transport, db):
    """After takeover, buyer messages must not trigger bot replies."""
    _patch_llm(engine)

    # Create dialog and trigger auto-takeover
    buyer_msg = make_msg("Есть Air M5?", dialog_id="chat_43")
    await engine.process_message(transport, buyer_msg)

    owner_msg = make_msg("Да, есть!", dialog_id="chat_43", is_owner=True)
    await engine.process_message(transport, owner_msg)

    # Buyer sends another message — bot must stay silent
    sent_before = len(transport.sent)
    follow_up = make_msg("Сколько стоит?", dialog_id="chat_43")
    result = await engine.process_message(transport, follow_up)

    assert result is None
    assert len(transport.sent) == sent_before, "No new messages should be sent"


# ── Manual takeover ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_takeover(engine, transport, db):
    """handle_takeover() should set status to owner_takeover with type=manual."""
    _patch_llm(engine)

    # Create dialog
    await engine.process_message(transport, make_msg("Hi", dialog_id="chat_44"))

    ok = await engine.handle_takeover("test", "chat_44", manual=True)
    assert ok is True

    dialog = await db.get_dialog("test", "chat_44")
    assert dialog["status"] == "owner_takeover"
    assert dialog["takeover_type"] == "manual"


@pytest.mark.asyncio
async def test_manual_takeover_nonexistent_dialog(engine):
    """handle_takeover on unknown dialog_id should return False."""
    ok = await engine.handle_takeover("test", "no_such_dialog")
    assert ok is False


# ── Resume ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_after_takeover(engine, transport, db):
    """After /start, the bot must respond again."""
    _patch_llm(engine, bot_text="Добрый день!")

    # Create dialog, takeover, then resume
    await engine.process_message(transport, make_msg("Привет", dialog_id="chat_45"))
    await engine.handle_takeover("test", "chat_45", manual=True)

    # Bot should be silent now
    result = await engine.process_message(transport, make_msg("Есть в наличии?", dialog_id="chat_45"))
    assert result is None

    # Resume
    ok = await engine.handle_resume("test", "chat_45")
    assert ok is True

    dialog = await db.get_dialog("test", "chat_45")
    assert dialog["status"] == "bot_active"

    # Bot should respond again
    _patch_llm(engine, bot_text="Да, есть 😊")
    result = await engine.process_message(transport, make_msg("Цену скажите", dialog_id="chat_45"))
    assert result == "Да, есть 😊"
