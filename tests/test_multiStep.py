"""
Multi-step scenario tests.

Covers the mechanisms that single-message tests can't verify:
  1. Full takeover lifecycle: buyer dialog → auto-takeover → bot silent → /start → resume
  2. Lead notification idempotency: two lead messages in < cooldown window → one notification
  3. Token limit alert at 80% of global daily cap
  4. History compression: 30+ messages → compress call is made
  5. Dual-trigger: bargain message fires both lead + escalation notifications
  6. Admin command output: /status and /dialogs via engine methods
  7. Silenced dialog: stays silent across multiple follow-up messages
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, call, patch

from src.core.dialog_engine import DialogEngine, _HISTORY_LIMIT
from tests.conftest import (
    FakeTransport,
    _bot_reply,
    _lead_response,
    _toxic_response,
    make_msg,
)


# ── Shared mock builder ───────────────────────────────────────────────────────

def _patch(engine: DialogEngine, bot_text: str = "OK", is_lead: bool = False,
           is_toxic: bool = False):
    async def side_effect(**kwargs):
        if kwargs.get("response_format") == {"type": "json_object"}:
            msgs = kwargs.get("messages", [])
            content = msgs[0]["content"] if msgs else ""
            if "токсич" in content.lower():
                return _toxic_response(is_toxic)
            return _lead_response(is_lead)
        return _bot_reply(bot_text)
    engine.client.chat.completions.create = AsyncMock(side_effect=side_effect)


# ── 1. Full takeover lifecycle ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_takeover_lifecycle(engine, transport, db):
    """
    5-step lifecycle:
      1. Buyer sends 3 messages → bot responds
      2. Owner message arrives → auto-takeover (bot stops)
      3. Buyer sends follow-up → bot stays silent
      4. Manual /start → bot resumes
      5. Buyer sends message → bot responds again
    """
    _patch(engine, "Добрый день!")

    # Phase 1: normal conversation
    for i, text in enumerate([
        "Добрый день! Есть MacBook Air M5?",
        "А в каком цвете?",
        "Серебристый? Отлично, сколько стоит?",
    ]):
        reply = await engine.process_message(transport, make_msg(text, dialog_id="tl1"))
        assert reply is not None, f"Message {i+1}: bot should respond"

    dialog = await db.get_dialog("test", "tl1")
    assert dialog["status"] == "bot_active"

    # Phase 2: auto-takeover
    owner_reply = await engine.process_message(
        transport,
        make_msg("Да, приходите, MacBook Air 13 M5 16/512 в наличии", dialog_id="tl1", is_owner=True)
    )
    assert owner_reply is None, "Bot must not respond to owner messages"

    dialog = await db.get_dialog("test", "tl1")
    assert dialog["status"] == "owner_takeover"
    assert dialog["takeover_type"] == "auto"
    print(f"\n[Такеовер] Статус после авто-такеовера: {dialog['status']} ({dialog['takeover_type']})")

    # Phase 3: buyer follow-up — bot silent
    sent_before = len(transport.sent)
    for text in ["Сколько берёте за него?", "Можно приехать сегодня?"]:
        result = await engine.process_message(transport, make_msg(text, dialog_id="tl1"))
        assert result is None, "Bot must stay silent after owner takeover"
    assert len(transport.sent) == sent_before, "No sends during takeover"
    print(f"[Такеовер] Бот молчит: {len(transport.sent) - sent_before} новых сообщений (ожидается 0)")

    # Phase 4: manual /start
    ok = await engine.handle_resume("test", "tl1")
    assert ok is True
    dialog = await db.get_dialog("test", "tl1")
    assert dialog["status"] == "bot_active"
    print(f"[Такеовер] После /start: {dialog['status']}")

    # Phase 5: bot responds again
    _patch(engine, "97 900 ₽ при оплате наличными 😊")
    reply = await engine.process_message(transport, make_msg("Итого цена?", dialog_id="tl1"))
    assert reply is not None
    print(f"[Такеовер] Бот снова отвечает: {reply}")


# ── 2. Lead notification idempotency ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_lead_notification_idempotency(engine, transport, db):
    """
    Two lead-triggering messages arrive within the cooldown window.
    Owner should receive exactly ONE notification, not two.
    """
    _patch(engine, "Передала запрос менеджеру 😊", is_lead=True)

    # First lead message
    await engine.process_message(
        transport, make_msg("Беру, подъеду завтра к 12", dialog_id="tl2")
    )
    first_count = len(transport.owner_notifications)
    assert first_count >= 1, "First lead must produce notification"

    lead_notifs = [n for n in transport.owner_notifications if "Прогретый лид" in n or "🔔" in n]
    assert len(lead_notifs) == 1
    print(f"\n[Идемпотентность] После 1-го лид-сообщения: {len(lead_notifs)} уведомление")

    # Second lead message within cooldown window
    await engine.process_message(
        transport, make_msg("Кстати, отложите пожалуйста на моё имя", dialog_id="tl2")
    )

    lead_notifs_after = [n for n in transport.owner_notifications if "Прогретый лид" in n or "🔔" in n]
    assert len(lead_notifs_after) == 1, \
        f"Second lead within cooldown must NOT produce new notification, got {len(lead_notifs_after)}"
    print(f"[Идемпотентность] После 2-го лид-сообщения: {len(lead_notifs_after)} уведомление (cooldown сработал)")


# ── 3. Token limit alert ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_limit_alert(engine, transport, db):
    """
    Pre-seed token usage to 82% of the global cap.
    Next message should trigger the 80% alert notification to the owner.
    """
    # Lower the global cap to a testable value and patch the engine
    engine.max_global_tokens = 1000

    _patch(engine, "Добрый день!")

    # Create the dialog first
    await engine.process_message(transport, make_msg("Привет", dialog_id="tl3"))
    dialog = await db.get_dialog("test", "tl3")

    # First message already added 150 tokens. Seed 700 more → total 850 (85% of 1000).
    # This keeps us below the hard cap so the second LLM call goes through,
    # but puts us above 80% threshold so the alert fires after that call.
    await db.record_token_usage(dialog["id"], 700, 0)

    # Clear notifications from the first message
    transport.owner_notifications.clear()

    # Send another message — total will exceed 80% → alert
    _patch(engine, "Да, есть!")
    await engine.process_message(transport, make_msg("Есть Air M5?", dialog_id="tl3"))

    alert_notifs = [n for n in transport.owner_notifications if "токен" in n.lower() or "лимит" in n.lower()]
    assert len(alert_notifs) >= 1, "Token alert must fire when global usage ≥ 80%"
    print(f"\n[Токены] Алерт: {alert_notifs[0][:150]}")


# ── 4. History compression ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_compression_triggered(engine, transport, db):
    """
    When total message count exceeds HISTORY_LIMIT, the engine must
    call the compress prompt before building the LLM context.
    """
    compress_calls = []

    async def side_effect(**kwargs):
        msgs = kwargs.get("messages", [])
        content = msgs[0]["content"] if msgs else ""

        if kwargs.get("response_format") == {"type": "json_object"}:
            if "токсич" in content.lower():
                return _toxic_response(False)
            return _lead_response(False)

        # Detect the compression call by its prompt content
        if "Резюме:" in content or "резюме диалога" in content.lower():
            compress_calls.append(kwargs)
            from tests.conftest import _make_completion
            return _make_completion("Покупатель интересовался MacBook Air M5, уточнял цену и наличие.")

        return _bot_reply("Добрый день!")

    engine.client.chat.completions.create = AsyncMock(side_effect=side_effect)

    # Pre-populate DB with HISTORY_LIMIT + 5 messages (exceeds threshold)
    dialog = await db.get_or_create_dialog("test", "tl4")
    dialog_id = dialog["id"]

    total_to_add = _HISTORY_LIMIT + 5  # 25 messages
    for i in range(total_to_add):
        role = "user" if i % 2 == 0 else "assistant"
        await db.add_message(dialog_id, role, f"Сообщение {i+1}")

    print(f"\n[Сжатие] Сообщений в DB перед запросом: {await db.get_message_count(dialog_id)}")

    # Now process one more message — should trigger compression
    result = await engine.process_message(
        transport,
        make_msg("Итого сколько стоит?", dialog_id="tl4")
    )

    assert result is not None
    assert len(compress_calls) >= 1, (
        f"Engine must call compress when total messages > {_HISTORY_LIMIT}, "
        f"got {len(compress_calls)} compress calls"
    )
    print(f"[Сжатие] Вызовов компрессии: {len(compress_calls)} (ожидается ≥1)")
    print(f"[Сжатие] Ответ бота после сжатой истории: {result}")


# ── 5. Dual-trigger: bargain = lead + escalation ──────────────────────────────

@pytest.mark.asyncio
async def test_dual_trigger_bargain_and_lead(engine, transport):
    """
    «Дайте скидку 5000, беру сразу два MacBook» must trigger BOTH:
    - Lead notification (because «беру два» = purchase intent)
    - Escalation notification (because bot escalates on price)
    """
    _patch(engine,
           "По цене лучше обсудить напрямую с менеджером — передам ваш вопрос 😊",
           is_lead=True)

    await engine.process_message(
        transport,
        make_msg("Дайте скидку 5000, беру сразу два MacBook", dialog_id="tl5")
    )

    lead_notifs = [n for n in transport.owner_notifications if "Прогретый лид" in n or "🔔" in n]
    escalation_notifs = [n for n in transport.owner_notifications if "Эскалация" in n or "📌" in n]

    assert len(lead_notifs) >= 1, "Bargain + «беру два» must fire lead notification"
    assert len(escalation_notifs) >= 1, "Escalation to manager must fire escalation notification"

    print(f"\n[Двойной триггер] Уведомлений всего: {len(transport.owner_notifications)}")
    print(f"[Двойной триггер]   - Лид: {len(lead_notifs)}")
    print(f"[Двойной триггер]   - Эскалация: {len(escalation_notifs)}")
    for n in transport.owner_notifications:
        print(f"\n{'─'*60}\n{n}")


# ── 6. Admin information methods ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_dialog_listing(engine, transport, db):
    """
    After several conversations, list_dialogs returns correct data
    and get_dialog returns accurate per-dialog stats.
    """
    _patch(engine, "Добрый день!")

    # Create 3 dialogs
    for i in range(3):
        await engine.process_message(transport, make_msg(f"Привет #{i}", dialog_id=f"admin_{i}"))

    # Trigger takeover on first
    await engine.handle_takeover("test", "admin_0", manual=True)

    dialogs = await db.list_dialogs(limit=10)
    assert len(dialogs) == 3

    statuses = {d["external_id"]: d["status"] for d in dialogs}
    assert statuses["admin_0"] == "owner_takeover"
    assert statuses["admin_1"] == "bot_active"
    assert statuses["admin_2"] == "bot_active"

    # Per-dialog stats
    d0 = await db.get_dialog("test", "admin_0")
    msg_count = await db.get_message_count(d0["id"])
    tokens = await db.get_daily_tokens(d0["id"])

    print(f"\n[Статус] Диалогов: {len(dialogs)}")
    for d in dialogs:
        print(f"  #{d['id']} {d['external_id']} → {d['status']}")

    print(f"\n[Статус] admin_0: {msg_count} сообщений, {tokens} токенов сегодня")
    assert msg_count >= 2  # user + assistant
    assert tokens > 0


# ── 7. Silenced dialog — multiple follow-ups ─────────────────────────────────

@pytest.mark.asyncio
async def test_silenced_dialog_auto_restores_on_clean_followup(engine, transport, db):
    """
    After toxicity silencing, the first non-toxic message auto-restores the dialog.
    Owner notification fires exactly once — no re-notifications for the clean follow-ups.
    """
    _patch(engine, is_toxic=True)

    await engine.process_message(transport, make_msg("[мат]", dialog_id="tl7"))

    dialog = await db.get_dialog("test", "tl7")
    assert dialog["status"] == "silenced"
    notif_count_after_toxic = len(transport.owner_notifications)

    # Switch to polite mode — first message restores; further ones are normal
    _patch(engine, "Добрый день!", is_toxic=False)

    followup_texts = [
        "Извините, я погорячился",
        "Хочу купить MacBook, у вас есть?",
        "Здравствуйте, подскажите цену",
    ]
    for text in followup_texts:
        result = await engine.process_message(transport, make_msg(text, dialog_id="tl7"))
        assert result is not None, f"Restored dialog should reply to: '{text}'"

    # Exactly one notification (from the toxic message), none for clean follow-ups
    assert len(transport.owner_notifications) == notif_count_after_toxic, \
        "No extra owner notifications for clean follow-ups"

    dialog = await db.get_dialog("test", "tl7")
    assert dialog["status"] == "bot_active"

    print(f"\n[Авторестор] Статус: {dialog['status']}")
    print(f"[Авторестор] Уведомлений: {notif_count_after_toxic} (только при первом токсичном)")
    print(f"[Авторестор] Ответов после восстановления: {len(followup_texts)} из {len(followup_texts)}")


# ── 8. Retention query ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retention_returns_dialog_after_silence(db):
    """Dialog with old bot reply and no user follow-up appears in retention list."""
    dlg = await db.get_or_create_dialog("avito", "ret_test_1")
    # Insert a bot reply timestamped 10 minutes ago
    await db._db.execute(
        "INSERT INTO messages (dialog_id, role, text, ts) VALUES (?, 'assistant', 'Ок!', datetime('now', '-10 minutes'))",
        (dlg["id"],),
    )
    await db._db.commit()

    results = await db.get_dialogs_for_retention(silence_minutes=5)
    ids = [r["id"] for r in results]
    assert dlg["id"] in ids


@pytest.mark.asyncio
async def test_retention_not_returned_when_user_replied(db):
    """If buyer replied after the bot, dialog must NOT appear in retention list."""
    dlg = await db.get_or_create_dialog("avito", "ret_test_2")
    await db._db.execute(
        "INSERT INTO messages (dialog_id, role, text, ts) VALUES (?, 'assistant', 'Ок!', datetime('now', '-10 minutes'))",
        (dlg["id"],),
    )
    await db._db.execute(
        "INSERT INTO messages (dialog_id, role, text, ts) VALUES (?, 'user', 'Ладно', datetime('now', '-2 minutes'))",
        (dlg["id"],),
    )
    await db._db.commit()

    results = await db.get_dialogs_for_retention(silence_minutes=5)
    ids = [r["id"] for r in results]
    assert dlg["id"] not in ids


@pytest.mark.asyncio
async def test_retention_not_returned_after_notification_sent(db):
    """After retention notification is recorded, dialog must NOT appear again."""
    dlg = await db.get_or_create_dialog("avito", "ret_test_3")
    await db._db.execute(
        "INSERT INTO messages (dialog_id, role, text, ts) VALUES (?, 'assistant', 'Ок!', datetime('now', '-10 minutes'))",
        (dlg["id"],),
    )
    await db._db.commit()
    # Record the retention notification (sent after the bot message)
    await db.record_notification(dlg["id"], "retention", {})

    results = await db.get_dialogs_for_retention(silence_minutes=5)
    ids = [r["id"] for r in results]
    assert dlg["id"] not in ids


@pytest.mark.asyncio
async def test_retention_not_returned_when_recent_bot_reply(db):
    """Dialog where bot replied just 1 minute ago must NOT appear (too soon)."""
    dlg = await db.get_or_create_dialog("avito", "ret_test_4")
    await db._db.execute(
        "INSERT INTO messages (dialog_id, role, text, ts) VALUES (?, 'assistant', 'Ок!', datetime('now', '-1 minutes'))",
        (dlg["id"],),
    )
    await db._db.commit()

    results = await db.get_dialogs_for_retention(silence_minutes=5)
    ids = [r["id"] for r in results]
    assert dlg["id"] not in ids
