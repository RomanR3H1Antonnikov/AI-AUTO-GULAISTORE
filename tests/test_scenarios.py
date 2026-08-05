"""
End-to-end scenario smoke tests (all LLM calls mocked).

Covers the 6 scenarios from the spec:
  1. Обычный вопрос по наличию
  2. Вопрос про оплату картой
  3. Торг
  4. «Беру, подъеду завтра» (двойной триггер: лид + эскалация)
  5. Мат / токсичность
  6. Товар не из каталога
"""

import json
import pytest
from unittest.mock import AsyncMock

from tests.conftest import (
    FakeTransport,
    _bot_reply,
    _lead_response,
    _toxic_response,
    make_msg,
)


def _make_side_effect(bot_text: str, is_lead: bool = False, is_toxic: bool = False):
    """
    Returns a side_effect function that routes mocked completions:
    - JSON-mode requests (classifier_model) → lead or toxicity response
    - Regular requests (llm_model) → bot reply
    """
    call_count = {"n": 0}

    async def side_effect(**kwargs):
        if kwargs.get("response_format") == {"type": "json_object"}:
            msgs = kwargs.get("messages", [])
            content = msgs[0]["content"] if msgs else ""
            if "токсич" in content.lower():
                return _toxic_response(is_toxic)
            return _lead_response(is_lead)
        return _bot_reply(bot_text)

    return side_effect


# ── Scenario 1: Вопрос по наличию ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_stock_question(engine, transport):
    """Покупатель спрашивает про MacBook Air M5 → ответ с оговоркой про наличие."""
    engine.client.chat.completions.create = AsyncMock(
        side_effect=_make_side_effect(
            "Да, эта модель у нас есть 😊 Актуальный остаток подтвержу перед вашим приездом",
            is_lead=False
        )
    )

    result = await engine.process_message(
        transport, make_msg("Добрый день! Есть MacBook Air 13 M5 16/512?", dialog_id="sc1")
    )

    assert result is not None
    assert "есть" in result.lower()
    assert len(transport.owner_notifications) == 0, "No notification for a simple stock query"

    print(f"\n[Сценарий 1] Бот: {result}")
    print(f"[Сценарий 1] Уведомлений: {len(transport.owner_notifications)}")


# ── Scenario 2: Вопрос про оплату картой ─────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_card_payment(engine, transport):
    """Вопрос про карту → бот объясняет наценку, лид НЕ срабатывает."""
    engine.client.chat.completions.create = AsyncMock(
        side_effect=_make_side_effect(
            "При оплате картой или СБП цена выше на 13% 💳 Цена в объявлении — при оплате наличными.",
            is_lead=False
        )
    )

    result = await engine.process_message(
        transport, make_msg("А можно оплатить картой? Сколько будет?", dialog_id="sc2")
    )

    assert result is not None
    assert "13" in result
    assert len(transport.owner_notifications) == 0, "Card question is NOT a lead"

    print(f"\n[Сценарий 2] Бот: {result}")
    print(f"[Сценарий 2] Уведомлений владельцу: {len(transport.owner_notifications)}")


# ── Scenario 3: Торг ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_bargain(engine, transport):
    """
    «Дайте скидку, беру сразу два» — торг И лид одновременно.
    Бот отказывает по цене, но «беру два» — явный сигнал покупки.
    Владелец должен получить ОБА уведомления: лид + эскалация по цене.
    """
    engine.client.chat.completions.create = AsyncMock(
        side_effect=_make_side_effect(
            "По цене лучше обсудить напрямую с менеджером — передам ваш вопрос 😊",
            is_lead=True   # «беру сразу два» = лид, даже внутри торга
        )
    )

    result = await engine.process_message(
        transport,
        make_msg("Дайте скидку 5000, беру сразу два MacBook", dialog_id="sc3")
    )

    assert result is not None
    assert "менеджер" in result.lower() or "передам" in result.lower()

    types = set()
    for n in transport.owner_notifications:
        if "Прогретый лид" in n or "🔔" in n:
            types.add("lead")
        if "Эскалация" in n or "📌" in n:
            types.add("escalation")

    assert "lead" in types, "«беру два» должен дать уведомление о лиде"
    assert "escalation" in types, "Ссылка на менеджера (торг) должна дать уведомление об эскалации"

    print(f"\n[Сценарий 3] Бот: {result}")
    for n in transport.owner_notifications:
        print(f"[Сценарий 3] Уведомление: {n[:150]}")
        print("---")


# ── Scenario 4: «Беру, подъеду завтра» ───────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_hot_lead(engine, transport):
    """
    «Беру! Подъеду завтра к 12, отложите» — покупатель уже попросил бронь.
    Правильный ответ: подтвердить что передала менеджеру, НЕ советовать забронировать.
    """
    engine.client.chat.completions.create = AsyncMock(
        side_effect=_make_side_effect(
            "Передала ваш запрос на бронирование менеджеру — он подтвердит и напишет вам 😊",
            is_lead=True
        )
    )

    result = await engine.process_message(
        transport,
        make_msg("Беру MacBook Air 13 M5! Подъеду завтра к 12, отложите пожалуйста", dialog_id="sc4")
    )

    assert result is not None
    # Бот должен подтвердить передачу, а не советовать бронировать
    assert "менеджер" in result.lower() or "передал" in result.lower()
    assert "рекомендую забронировать" not in result, \
        "Бот не должен советовать бронь тому, кто уже попросил её"

    lead_notifs = [n for n in transport.owner_notifications if "Прогретый лид" in n or "🔔" in n]
    assert len(lead_notifs) >= 1, "Hot lead must trigger owner notification"

    notif = lead_notifs[0]
    assert "sc4" in notif or "#" in notif
    print(f"\n[Сценарий 4] Бот: {result}")
    print(f"[Сценарий 4] Уведомление лида:\n{notif}")


# ── Scenario 5: Мат / токсичность ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_toxicity(engine, transport, db):
    """Токсичное сообщение → бот молчит, владелец уведомлён, диалог silenced."""
    engine.client.chat.completions.create = AsyncMock(
        side_effect=_make_side_effect(
            "Этот ответ не должен дойти",
            is_toxic=True
        )
    )

    result = await engine.process_message(
        transport,
        make_msg("[нецензурное сообщение с оскорблениями]", dialog_id="sc5")
    )

    assert result is None, "Bot must stay silent on toxic message"

    dialog = await db.get_dialog("test", "sc5")
    assert dialog["status"] == "silenced"

    assert len(transport.owner_notifications) == 1
    notif = transport.owner_notifications[0]
    assert "токсич" in notif.lower() or "замолч" in notif.lower()

    print(f"\n[Сценарий 5] Ответ бота: None (молчит)")
    print(f"[Сценарий 5] Статус диалога: {dialog['status']}")
    print(f"[Сценарий 5] Уведомление: {notif[:120]}...")


# ── Scenario 6: Товар не из каталога ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_scenario_unknown_product(engine, transport):
    """Запрос товара вне каталога → эскалация + уведомление владельца."""
    engine.client.chat.completions.create = AsyncMock(
        side_effect=_make_side_effect(
            "Уточню у коллег и вернусь с ответом",
            is_lead=False
        )
    )

    result = await engine.process_message(
        transport,
        make_msg("У вас есть iPhone 16 Pro 256GB? Сколько стоит?", dialog_id="sc6")
    )

    assert result is not None
    assert "коллег" in result.lower() or "уточн" in result.lower()
    assert len(transport.owner_notifications) >= 1

    print(f"\n[Сценарий 6] Бот: {result}")
    print(f"[Сценарий 6] Уведомление: {transport.owner_notifications[0][:120]}...")
