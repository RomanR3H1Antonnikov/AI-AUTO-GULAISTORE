"""
Transport-agnostic dialog engine.
This module knows nothing about Telegram or Avito — it works via the Transport interface.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml
from openai import AsyncOpenAI

from .lead_detector import LeadDetector
from .stock_source import StockSource
from .toxicity_detector import ToxicityDetector
from .transport import IncomingMessage, Transport
from ..storage.database import Database

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 20
_COMPRESS_THRESHOLD = 30
_LEAD_COOLDOWN_MINUTES = 30

# If the bot's own reply contains these patterns it means an escalation happened.
_ESCALATION_RE = re.compile(
    r"(передам\s+ваш\s+вопрос|уточн[а-я]+\s+у\s+коллег)", re.IGNORECASE
)

_SYSTEM_PROMPT = """\
Ты — помощница магазина Gulai Store. Отвечаешь покупателям в чате Авито.

═══ ПРАВИЛА ПОВЕДЕНИЯ ═══
• Называй себя «помощница магазина» — без имени.
• Не раскрывай, что ты ИИ или бот; не притворяйся живым человеком.
• Обращайся только на «вы».
• На приветствие («привет», «здравствуйте», «добрый день», «ку», «yo», «хай», «хэй», «дарова», «здрасьте» и любое другое приветствие) — только «Добрый день».
• Если в диалоге уже было приветствие и ты уже ответила «Добрый день» — НЕ повторяй его снова. Переходи сразу к сути следующего ответа.
• Тон: профессионально-дружелюбный, 1–2 смайлика на сообщение максимум.
• Отвечай КРАТКО: 2–5 предложений. Это чат, не лендинг.
• Не добавляй вводных фраз («Конечно!», «Отличный вопрос!»).
• ФОРМАТ: только обычный текст. Никакого Markdown — не используй **, *, #, _, ~, `, > и другие символы разметки. Telegram отображает их как обычные символы без форматирования.

═══ МАГАЗИН ═══
{knowledge_base}

═══ КАТАЛОГ ТОВАРОВ ═══
{catalog}

═══ НАЛИЧИЕ ТОВАРА — ТРИ СЛУЧАЯ ═══

Случай 1 — позиция ЕСТЬ В КАТАЛОГЕ выше:
  → «Да, эта модель у нас есть 😊 Актуальный остаток подтвержу перед вашим приездом»
  НЕ говори «точно есть» или «гарантированно в наличии».

Случай 2 — это ТЕХНИКА APPLE, но её НЕТ в нашем каталоге
  (примеры: iPhone, iPad, AirPods, Apple Watch, Mac mini, Mac Pro, аксессуары Apple, кабели, чехлы):
  → «Уточню у коллег и вернусь с ответом» — и ничего больше не придумывай.

Случай 3 — это ЯВНО НЕ Apple и не наш профиль:
  (примеры: автомобили, одежда, еда, смартфоны других брендов, несуществующие модели,
   бессмысленные запросы вроде «трусы Егорыча», «BMW M5 Competition»)
  → Вежливо объясни специализацию:
  «Мы специализируемся на технике Apple — MacBook и iMac 😊 Если интересует что-то из этой линейки, помогу!»
  НЕ говори «уточню у коллег» — это явно не наш товар.

═══ ЦВЕТ / КОНФИГУРАЦИЯ ═══
Когда покупатель спрашивает о доступных цветах или конфигурациях:
• Называй ТОЛЬКО те цвета и конфигурации, которые есть в каталоге выше для конкретной модели.
• НЕ говори «все цвета доступны» и НЕ предлагай «скинуть прайс» — вся актуальная информация уже в каталоге.
• Если нужного цвета нет в каталоге → «Уточню у коллег насчёт других цветов — вернусь с ответом».

═══ ОПЛАТА ═══
• Цена в объявлении — при оплате НАЛИЧНЫМИ.
• Карта / СБП / безнал для юрлиц — +13% к цене.
• Про наценку упоминай ТОЛЬКО если покупатель спрашивает про безнал/карту/перевод.
• Если покупатель СПРАШИВАЕТ «можно ли картой?» / «принимаете СБП?» — СНАЧАЛА «Да, принимаем 😊», ПОТОМ условие:
  «Да, принимаем 😊 При оплате картой или СБП к цене добавляется 13%.»

═══ БРОНИРОВАНИЕ ═══
Бронь оформляет менеджер. Различай два разных сигнала:

А) Покупатель ПРОСИТ БРОНЬ — явные слова «отложите», «забронируйте», «зарезервируйте», «придержите»:
   → «Передала ваш запрос на бронирование менеджеру — он подтвердит и напишет вам 😊»
   НЕ советуй «рекомендую забронировать» — он только что это сделал.

Б) Покупатель ВЫРАЖАЕТ НАМЕРЕНИЕ КУПИТЬ — «хочу купить», «беру», «готов взять», «куплю»:
   → Это НЕ просьба о брони. Помоги с следующим шагом: уточни модель (если не назвал),
     напомни что перед приездом нужно забронировать, спроси когда удобно приехать.
   Пример: «Отлично! 😊 Уточните, какая модель вас интересует, — подберём и забронируем для вас.»

═══ ПАРАЛЛЕЛЬНЫЙ ИМПОРТ ═══
Если спрашивают «это серый?» / «официальный?» / «откуда техника?»:
  → Говори прямо: техника ввезена по параллельному импорту, новая, с гарантией магазина 12 мес. и кассовым чеком.
  Не юли, не уходи от вопроса — честность продаёт лучше.

═══ РАСХОЖДЕНИЕ ЦЕН ═══
Если цена в объявлении отличается от цены в каталоге:
→ «Да, в объявлении вышла ошибка с ценой — актуальная цена [X] ₽, уже исправляем. Скажите, устраивает вас эта цена?»
Не спорь, не оправдывайся долго.

═══ ПЕРЕГОВОРЫ О ЦЕНЕ ═══
Если покупатель говорит «дорого» / «хочу скидку» / «уступите»:
→ «Подскажите, какую минимальную цену нашли? Мы готовы предложить лучшую цену 😊»

Если покупатель называет конкурентную цену ИЛИ явно собирается уйти:
→ Предложи скидку РОВНО 500 ₽ и НЕ БОЛЬШЕ:
  «Специально для вас сделаем скидку 500 ₽ — итого [цена из каталога − 500] ₽ 😊»

Если покупатель отказывается даже от скидки 500 ₽ и продолжает торговаться:
→ «По вопросам торга лучше обсудить с менеджером — передам ваш вопрос 😊»

ВАЖНО: скидку более 500 ₽ предлагать НЕЛЬЗЯ.

═══ ЗАПРЕЩЕНО ═══
• Давать скидку более 500 ₽ (см. раздел ПЕРЕГОВОРЫ О ЦЕНЕ).
• Бронировать самостоятельно (только передавать запрос, см. раздел БРОНИРОВАНИЕ).
• Выдумывать характеристики, комплектацию, сроки поставки.
  Если просят «перечисли все характеристики» — назови то, что знаешь из каталога (чип, RAM, SSD),
  и добавь: «Полные технические характеристики — на apple.com 😊»
• Обещать конкретное время доставки или приезда курьера.
• Обсуждать конкурентов или сравнивать магазины.

═══ ТЕКУЩЕЕ ВРЕМЯ ═══
{current_dt}"""

_COMPRESS_PROMPT = """\
Составь краткое резюме диалога (2–4 предложения) для продолжения разговора.
Включи: что интересовало покупателя, какие товары обсуждались, на каком этапе остановились.

История:
{messages}

Резюме:"""


class DialogEngine:
    """
    Core engine — stateless relative to transports.
    All state lives in the Database.
    """

    def __init__(
        self,
        db: Database,
        openai_client: AsyncOpenAI,
        knowledge_base_path: str,
        catalog_path: str,
        config: dict,
        stock_source: StockSource,
    ) -> None:
        self.db = db
        self.client = openai_client
        self.config = config
        self.stock_source = stock_source

        clf_model = config.get("classifier_model", "gpt-4o-mini")
        self.lead_detector = LeadDetector(openai_client, clf_model)
        self.toxicity_detector = ToxicityDetector(openai_client, clf_model)

        self.llm_model: str = config.get("llm_model", "gpt-4o")
        self.max_dialog_tokens: int = config.get("max_tokens_per_dialog_day", 10_000)
        self.max_global_tokens: int = config.get("max_tokens_global_day", 500_000)

        with open(knowledge_base_path, encoding="utf-8") as f:
            self._kb = yaml.safe_load(f)
        with open(catalog_path, encoding="utf-8") as f:
            self._cat = yaml.safe_load(f)

    # ── Prompt helpers ────────────────────────────────────────────────────────

    def _format_kb(self) -> str:
        s = self._kb.get("store", {})
        lines = [
            f"Название: {s.get('name', '')}",
            f"Расположение: {s.get('location', '')}, {s.get('address', '')}",
            f"Метро: {s.get('metro', '')}",
            f"Режим работы: {s.get('hours', '')}",
            f"Рейтинг: {s.get('rating', '')} ({s.get('reviews_count', '')} отзывов)",
            "",
        ]
        for fact in self._kb.get("facts", []):
            lines.append(f"• {fact}")
        return "\n".join(lines)

    def _format_catalog(self) -> str:
        cat_notes = self._cat.get("category_notes", {})
        lines: list[str] = []
        for category, items in self._cat.get("categories", {}).items():
            note = cat_notes.get(category, "")
            header = f"\n{category}" + (f" [{note}]" if note else "") + ":"
            lines.append(header)
            for item in items:
                parts = [f"  • {item['name']}"]
                if item.get("config"):
                    parts.append(item["config"])
                if item.get("color"):
                    parts.append(f"({item['color']})")
                price = f"{item['price']:,}".replace(",", " ")
                parts.append(f"— {price} ₽")
                lines.append(" ".join(parts))
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT.format(
            knowledge_base=self._format_kb(),
            catalog=self._format_catalog(),
            current_dt=datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M"),
        )

    # ── History / context ─────────────────────────────────────────────────────

    async def _compress_history(self, messages: list[dict]) -> str:
        text = "\n".join(
            f"{'ПОКУПАТЕЛЬ' if m['role'] == 'user' else 'БОТ'}: {m['text']}"
            for m in messages
        )
        resp = await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": _COMPRESS_PROMPT.format(messages=text)}],
            temperature=0,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()

    async def _build_llm_messages(self, dialog_id: int) -> list[dict]:
        total = await self.db.get_message_count(dialog_id)
        recent = await self.db.get_messages(dialog_id, limit=_HISTORY_LIMIT)

        result: list[dict] = []

        if total > _HISTORY_LIMIT and len(recent) == _HISTORY_LIMIT:
            all_msgs = await self.db.get_messages(dialog_id, limit=total)
            older = all_msgs[:-_HISTORY_LIMIT]
            if older:
                summary = await self._compress_history(older)
                result.append({"role": "system", "content": f"[Резюме предыдущей части диалога]: {summary}"})

        for m in recent:
            role = "assistant" if m["role"] == "assistant" else "user"
            result.append({"role": role, "content": m["text"]})

        return result

    # ── Token limits ──────────────────────────────────────────────────────────

    async def _within_token_limits(self, dialog_id: int) -> bool:
        d_tokens = await self.db.get_daily_tokens(dialog_id)
        if d_tokens >= self.max_dialog_tokens:
            logger.warning("dialog %d hit daily token cap (%d)", dialog_id, d_tokens)
            return False
        g_tokens = await self.db.get_daily_tokens()
        if g_tokens >= self.max_global_tokens:
            logger.warning("global daily token cap hit (%d)", g_tokens)
            return False
        return True

    async def _maybe_token_alert(self, transport: Transport, dialog_id: int) -> None:
        g_tokens = await self.db.get_daily_tokens()
        threshold = int(self.max_global_tokens * 0.8)
        if g_tokens >= threshold:
            last = await self.db.get_last_notification(dialog_id, "token_alert")
            if not last:
                await transport.send_owner_notification(
                    f"⚠️ Использовано {g_tokens:,} токенов сегодня (80% дневного лимита).\n"
                    f"Лимит: {self.max_global_tokens:,}"
                )
                await self.db.record_notification(dialog_id, "token_alert",
                                                  {"global_tokens": g_tokens})

    # ── Notifications ─────────────────────────────────────────────────────────

    async def _notify_lead(
        self,
        transport: Transport,
        dialog: dict,
        message_text: str,
        reason: str,
    ) -> None:
        dialog_id = dialog["id"]
        last = await self.db.get_last_notification(dialog_id, "lead")
        if last:
            sent = datetime.fromisoformat(last["sent_at"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - sent < timedelta(minutes=_LEAD_COOLDOWN_MINUTES):
                logger.info("lead notification for dialog %d skipped (cooldown)", dialog_id)
                return

        recent = await self.db.get_messages(dialog_id, limit=6)
        ctx = "\n".join(
            f"{'→' if m['role'] == 'user' else '←'} {m['text']}"
            for m in recent[-4:]
        )

        text = (
            f"🔔 Прогретый лид!\n\n"
            f"Диалог #{dialog_id} | {transport.name}\n"
            f"Клиент: {dialog['external_id']}\n"
            f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n\n"
            f"💬 Сообщение: «{message_text[:200]}»\n"
            f"📝 Причина: {reason}\n\n"
            f"Контекст:\n{ctx}"
        )
        await transport.send_owner_notification(text)
        await self.db.record_notification(dialog_id, "lead",
                                          {"reason": reason, "msg": message_text[:200]})
        logger.info("lead notification sent for dialog %d", dialog_id)

    async def _notify_escalation(
        self,
        transport: Transport,
        dialog: dict,
        user_message: str,
        bot_reply: str,
    ) -> None:
        """Called when the bot replies with escalation phrases (торг, unknown product)."""
        dialog_id = dialog["id"]
        last = await self.db.get_last_notification(dialog_id, "escalation")
        if last:
            sent = datetime.fromisoformat(last["sent_at"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - sent < timedelta(minutes=10):
                return

        text = (
            f"📌 Эскалация в диалоге #{dialog_id}\n\n"
            f"Клиент: {dialog['external_id']}\n"
            f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n\n"
            f"Вопрос клиента: «{user_message[:200]}»\n"
            f"Ответ бота: «{bot_reply[:200]}»"
        )
        await transport.send_owner_notification(text)
        await self.db.record_notification(dialog_id, "escalation",
                                          {"user_msg": user_message[:200]})

    # ── Main entry point ──────────────────────────────────────────────────────

    async def process_message(
        self,
        transport: Transport,
        message: IncomingMessage,
    ) -> Optional[str]:
        """
        Process one incoming message. Returns the reply text, or None to stay silent.
        This method is the ONLY place where dialog state is mutated.
        """
        # 1. Resolve / create dialog row
        dialog = await self.db.get_or_create_dialog(transport.name, message.dialog_id)
        dialog_id: int = dialog["id"]

        # 2. Owner message → auto-takeover; never respond
        if message.is_owner_message:
            if dialog["status"] == "bot_active":
                await self.db.update_dialog_status(dialog_id, "owner_takeover", "auto")
                logger.info("auto takeover: dialog %d owner=%s", dialog_id, message.sender_id)
            return None

        # 3. Respect current dialog status
        status = dialog["status"]
        if status not in ("bot_active", "silenced"):
            logger.debug("dialog %d is %s — silent", dialog_id, status)
            return None

        # 4. Persist incoming message
        await self.db.add_message(dialog_id, "user", message.text)

        # 5. Toxicity gate
        is_toxic, toxic_reason = await self.toxicity_detector.classify(message.text)
        if is_toxic:
            if status == "bot_active":
                logger.info("toxic message in dialog %d: %s", dialog_id, toxic_reason)
                await self.db.update_dialog_status(dialog_id, "silenced")
                await transport.send_owner_notification(
                    f"⚠️ Токсичное сообщение — бот замолчал\n\n"
                    f"Диалог #{dialog_id} | {dialog['external_id']}\n"
                    f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n"
                    f"Причина: {toxic_reason}\n"
                    f"Сообщение: «{message.text[:300]}»\n\n"
                    f"Бот автоматически возобновит ответы, как только клиент напишет адекватно."
                )
            else:
                logger.debug("dialog %d still silenced (another toxic message)", dialog_id)
            return None

        # If silenced but this message is clean → auto-restore
        if status == "silenced":
            logger.info("dialog %d: auto-restored after non-toxic follow-up", dialog_id)
            await self.db.update_dialog_status(dialog_id, "bot_active", None)

        # 6. Token limit gate
        if not await self._within_token_limits(dialog_id):
            return None

        # 7. Build context and call LLM
        system_prompt = self._build_system_prompt()
        history = await self._build_llm_messages(dialog_id)
        llm_msgs = [{"role": "system", "content": system_prompt}] + history

        response = await self.client.chat.completions.create(
            model=self.llm_model,
            messages=llm_msgs,
            temperature=0.7,
            max_tokens=500,
        )
        reply = response.choices[0].message.content.strip()
        usage = response.usage

        # 8. Track token usage and alert if needed
        await self.db.record_token_usage(dialog_id, usage.prompt_tokens, usage.completion_tokens)
        await self._maybe_token_alert(transport, dialog_id)

        # 9. Persist reply
        await self.db.add_message(dialog_id, "assistant", reply)

        # 10. Lead detection (on user message)
        is_lead, lead_reason = await self.lead_detector.classify(message.text)
        if is_lead:
            await self._notify_lead(transport, dialog, message.text, lead_reason)

        # 11. Escalation detection (on bot reply — торг, unknown product)
        if _ESCALATION_RE.search(reply):
            await self._notify_escalation(transport, dialog, message.text, reply)

        logger.info(
            "dialog=%d tokens=%d reply=%r",
            dialog_id, usage.total_tokens, reply[:80],
        )
        return reply

    # ── Admin commands ────────────────────────────────────────────────────────

    async def handle_takeover(
        self, transport_name: str, external_id: str, manual: bool = True
    ) -> bool:
        """Returns True if dialog found and status changed."""
        dialog = await self.db.get_dialog(transport_name, external_id)
        if not dialog:
            return False
        await self.db.update_dialog_status(
            dialog["id"], "owner_takeover", "manual" if manual else "auto"
        )
        return True

    async def handle_resume(self, transport_name: str, external_id: str) -> bool:
        """Resume bot after takeover. Returns True if dialog found."""
        dialog = await self.db.get_dialog(transport_name, external_id)
        if not dialog:
            return False
        await self.db.update_dialog_status(dialog["id"], "bot_active", None)
        return True
