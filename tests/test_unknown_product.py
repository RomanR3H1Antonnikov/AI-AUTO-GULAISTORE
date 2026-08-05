"""
Tests for unknown product handling and escalation notifications.

Three-way product classification:
  1. In catalog → hedged stock reply, no notification
  2. Apple product not in catalog (iPhone, iPad, AirPods…) → 'Уточню у коллег' + escalation notification
  3. Clearly not Apple / nonsense (BMW, clothes, food, fake models) → polite redirect, NO notification
"""

import pytest
from unittest.mock import AsyncMock

from tests.conftest import (
    _bot_reply,
    _lead_response,
    _toxic_response,
    make_msg,
)

ESCALATION_REPLY = "Уточню у коллег и вернусь с ответом"
STOCK_REPLY = "Да, эта модель у нас есть 😊 Актуальный остаток подтвержу перед вашим приездом"
BARGAIN_REPLY = "По цене лучше обсудить напрямую с менеджером — передам ваш вопрос 😊"
# Case 3: clearly not Apple — no "уточню у коллег", no owner notification
OFF_TOPIC_REPLY = "Мы специализируемся на технике Apple — MacBook и iMac 😊 Если интересует что-то из этой линейки, помогу!"


def _patch(engine, bot_text: str):
    async def side_effect(**kwargs):
        if kwargs.get("response_format") == {"type": "json_object"}:
            msgs = kwargs.get("messages", [])
            content = msgs[0]["content"] if msgs else ""
            if "токсич" in content.lower():
                return _toxic_response(False)
            return _lead_response(False)
        return _bot_reply(bot_text)
    engine.client.chat.completions.create = AsyncMock(side_effect=side_effect)


# ── Unknown product → escalation phrase → owner notified ─────────────────────

@pytest.mark.asyncio
async def test_unknown_product_triggers_owner_notification(engine, transport):
    _patch(engine, ESCALATION_REPLY)

    result = await engine.process_message(
        transport, make_msg("У вас есть iPhone 16 Pro?", dialog_id="chat_up1")
    )

    assert result == ESCALATION_REPLY
    assert len(transport.owner_notifications) == 1
    notif = transport.owner_notifications[0]
    assert "chat_up1" in notif or "#" in notif  # dialog reference present


@pytest.mark.asyncio
async def test_ipad_question_triggers_escalation(engine, transport):
    _patch(engine, "Уточню у коллег и вернусь с ответом")

    result = await engine.process_message(
        transport, make_msg("iPad Air есть?", dialog_id="chat_up2")
    )
    assert result is not None
    assert len(transport.owner_notifications) == 1


@pytest.mark.asyncio
async def test_accessories_question_triggers_escalation(engine, transport):
    _patch(engine, ESCALATION_REPLY)

    result = await engine.process_message(
        transport, make_msg("AirPods Pro 2 продаёте?", dialog_id="chat_up3")
    )
    assert result is not None
    assert len(transport.owner_notifications) == 1


# ── Bargaining → escalation phrase → owner notified ──────────────────────────

@pytest.mark.asyncio
async def test_bargain_triggers_owner_notification(engine, transport):
    _patch(engine, BARGAIN_REPLY)

    result = await engine.process_message(
        transport, make_msg("А скидку можно? Беру сразу, если цену снизите", dialog_id="chat_up4")
    )
    assert result == BARGAIN_REPLY
    # Escalation notification fired
    assert len(transport.owner_notifications) >= 1


# ── Known product → no escalation notification ───────────────────────────────

@pytest.mark.asyncio
async def test_known_product_no_escalation(engine, transport):
    _patch(engine, STOCK_REPLY)

    await engine.process_message(
        transport, make_msg("Есть MacBook Air 13 M5?", dialog_id="chat_up5")
    )

    # Stock reply doesn't contain escalation phrase → no owner notification
    assert len(transport.owner_notifications) == 0, (
        f"Should not notify owner for stock question. Got: {transport.owner_notifications}"
    )


# ── Case 3: clearly off-topic → polite redirect, NO owner notification ───────

@pytest.mark.asyncio
@pytest.mark.parametrize("message,desc", [
    ("BMW M5 Competition есть?",  "автомобиль"),
    ("Трусы Егорыча есть?",       "явный нонсенс"),
    ("Samsung Galaxy S25 есть?",  "другой бренд"),
    ("Айфон 19 про макс",         "несуществующая модель"),
])
async def test_off_topic_no_owner_notification(engine, transport, message, desc):
    """
    Clearly non-Apple / nonsense queries must get a polite redirect
    and must NOT trigger an escalation notification to the owner.
    """
    _patch(engine, OFF_TOPIC_REPLY)

    result = await engine.process_message(
        transport, make_msg(message, dialog_id=f"chat_ot_{desc[:4]}")
    )

    assert result is not None
    # No "уточню у коллег" phrase → no escalation notification
    assert "уточню у коллег" not in result.lower(), \
        f"Off-topic reply must not say 'уточню у коллег' for: {message}"
    assert len(transport.owner_notifications) == 0, \
        f"Owner must NOT be notified for off-topic query '{message}', got: {transport.owner_notifications}"


# ── Escalation cooldown ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_escalation_cooldown(engine, transport):
    """Two rapid escalations in the same dialog should fire only one notification."""
    _patch(engine, ESCALATION_REPLY)

    await engine.process_message(
        transport, make_msg("iPhone есть?", dialog_id="chat_up6")
    )
    initial_count = len(transport.owner_notifications)

    await engine.process_message(
        transport, make_msg("А iPad есть?", dialog_id="chat_up6")
    )

    # Second escalation within cooldown window → still same count
    assert len(transport.owner_notifications) == initial_count, (
        "Escalation notifications should be deduplicated within cooldown window"
    )
