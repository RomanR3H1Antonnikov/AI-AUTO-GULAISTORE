# -*- coding: utf-8 -*-
"""Patch dialog_engine.py: store escalation relay tg_msg_id + add reformulate method."""
FILEPATH = "src/core/dialog_engine.py"

with open(FILEPATH, encoding="utf-8") as f:
    src = f.read()

# ── 1. Patch _notify_escalation to capture tg_msg_id and store relay ──────────

OLD_TAIL = (
    '    text = (\n'
    '        f"\U0001f4cc Эскалация в диалоге #{dialog_id}\\n\\n"\n'
    '        f"Клиент: {dialog[\'external_id\']}\\n"\n'
    '        f"Ссылка: {transport.get_dialog_link(dialog[\'external_id\'])}\\n\\n"\n'
    '        f"Вопрос клиента: \xab{user_message[:200]}\xbb\\n"\n'
    '        f"Ответ бота: \xab{bot_reply[:200]}\xbb"\n'
    '    )\n'
    '    await transport.send_owner_notification(text)\n'
    '    await self.db.record_notification(dialog_id, "escalation",\n'
    '                                      {"user_msg": user_message[:200]})'
)

NEW_TAIL = (
    '    context = (\n'
    '        f"Вопрос клиента: \xab{user_message[:300]}\xbb\\n"\n'
    '        f"Ответ бота: \xab{bot_reply[:200]}\xbb"\n'
    '    )\n'
    '    text = (\n'
    '        f"\U0001f4cc Эскалация в диалоге #{dialog_id}\\n\\n"\n'
    '        f"Клиент: {dialog[\'external_id\']}\\n"\n'
    '        f"Ссылка: {transport.get_dialog_link(dialog[\'external_id\'])}\\n\\n"\n'
    '        f"Вопрос клиента: \xab{user_message[:200]}\xbb\\n"\n'
    '        f"Ответ бота: \xab{bot_reply[:200]}\xbb"\n'
    '    )\n'
    '    tg_msg_id = await transport.send_owner_notification(text)\n'
    '    await self.db.record_notification(dialog_id, "escalation",\n'
    '                                      {"user_msg": user_message[:200]})\n'
    '    if tg_msg_id is not None:\n'
    '        await self.db.store_escalation_relay(\n'
    '            tg_msg_id=tg_msg_id,\n'
    '            dialog_id=dialog_id,\n'
    '            transport=dialog["transport"],\n'
    '            external_id=dialog["external_id"],\n'
    '            context=context,\n'
    '        )'
)

if OLD_TAIL not in src:
    # Try to find with actual UTF-8 characters by reading literal
    print("Trying literal search...")
    # Print what we find around 'send_owner_notification'
    idx = src.find('await transport.send_owner_notification(text)')
    print(f"Found at index: {idx}")
    if idx >= 0:
        print(repr(src[idx-300:idx+100]))
    print("ERROR: Could not find _notify_escalation tail")
    exit(1)

src = src.replace(OLD_TAIL, NEW_TAIL, 1)
print("Patch 1 applied: _notify_escalation updated")

# ── 2. Add reformulate_owner_reply before handle_takeover ─────────────────────

REFORMULATE = '''
    async def reformulate_owner_reply(self, owner_text: str, context: str) -> str:
        """Переформулирует сырой ответ владельца в сообщение для покупателя."""
        prompt = (
            "Ты — помощница магазина Gulai Store. "
            "Владелец дал ответ на вопрос покупателя. "
            "Переформулируй его ответ в дружелюбное сообщение покупателю. "
            "Без вводных фраз, без Markdown, 1–3 предложения максимум. "
            "Обращайся на \xabвы\xbb.\\n\\n"
            f"Контекст:\\n{context}\\n\\n"
            f"Ответ владельца: \xab{owner_text}\xbb\\n\\n"
            "Сформулируй ответ покупателю:"
        )
        response = await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()

'''

ANCHOR = "    # ── Admin commands ────────────────────────────────────────────────"

if ANCHOR not in src:
    print("ERROR: Could not find admin commands anchor")
    exit(1)

src = src.replace(ANCHOR, REFORMULATE + ANCHOR, 1)
print("Patch 2 applied: reformulate_owner_reply method added")

with open(FILEPATH, "w", encoding="utf-8") as f:
    f.write(src)

print("\nVerifying...")
checks = [
    "tg_msg_id = await transport.send_owner_notification(text)",
    "store_escalation_relay",
    "reformulate_owner_reply",
    "gpt-4o-mini",
    "context",
]
for phrase in checks:
    found = phrase in src
    print(f"  {'OK' if found else 'FAIL'}  {phrase!r}")
