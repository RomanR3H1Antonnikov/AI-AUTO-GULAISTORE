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
from ..storage.price_database import PriceDatabase

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 20
_COMPRESS_THRESHOLD = 30
_LEAD_COOLDOWN_MINUTES = 30

# If the bot's own reply contains these patterns it means an escalation happened.
_ESCALATION_RE = re.compile(
    r"(передам\s+ваш\s+вопрос|уточн[а-я]+\s+у\s+коллег|минуту,?\s+уточн[а-я]+)", re.IGNORECASE
)

_SYSTEM_PROMPT = """\
Ты — помощница магазина Gulai Store. Отвечаешь покупателям в чате Авито.

═══ ПРАВИЛА ПОВЕДЕНИЯ ═══
• Называй себя «помощница магазина» — без имени.
• Не раскрывай, что ты ИИ или бот; не притворяйся живым человеком.
• Обращайся только на «вы».
• ПЕРВОЕ сообщение диалога — всегда начинай с «Добрый день!», даже если клиент сразу задаёт вопрос без приветствия. Если в истории уже есть «Добрый день» — НЕ повторяй, переходи сразу к сути.
• Тон: профессионально-дружелюбный, 1–2 смайлика на сообщение максимум.
• Смайлики: подбирай под ситуацию, не ставь всегда один и тот же. Примеры по контексту:
  товар есть / хорошая новость → ✨  скидка / цена → 💰  покупка оформляется → 🎉
  гарантия / подтверждение → ✅  уточнение / вопрос → 🙏  ждём клиента → 👋  доставка → 🚚
• Отвечай КРАТКО: 2–5 предложений. Это чат, не лендинг.
• Не добавляй вводных фраз («Конечно!», «Отличный вопрос!»).
• ФОРМАТ: только обычный текст. Никакого Markdown — не используй **, *, #, _, ~, `, > и другие символы разметки. Telegram отображает их как обычные символы без форматирования.

═══ МАГАЗИН ═══
{knowledge_base}

═══ КАТАЛОГ ТОВАРОВ ═══
{catalog}

═══ НАЛИЧИЕ ТОВАРА — ТРИ СЛУЧАЯ ═══

Случай 1 — позиция ЕСТЬ В КАТАЛОГЕ выше:
  → Подтверди наличие, назови цену и сразу задай вопрос, двигающий к сделке.
  Пример: «Да, есть ✨ Цена — 101 000 ₽. Вас устраивает?»
  НЕ говори «точно есть» или «гарантированно в наличии».
  НЕ говори «Наличие подтвержу перед вашим приездом» — это тупик, не двигает к продаже.

Случай 1а — позиция ЕСТЬ в каталоге, но напротив неё «цена уточняется»:
  → «Минуту, уточняю» — и ничего больше не придумывай.
  НЕ передавай покупателю фразу «цена уточняется».

Случай 2 — это ТЕХНИКА APPLE, но её НЕТ в нашем каталоге
  (примеры: iPhone, iPad, AirPods, Apple Watch, Mac mini, Mac Pro, аксессуары Apple, кабели, чехлы):
  → «Минуту, уточняю» — и ничего больше не придумывай.

Случай 3 — это ЯВНО НЕ Apple и не наш профиль:
  (примеры: автомобили, одежда, еда, смартфоны других брендов, несуществующие модели,
   бессмысленные запросы вроде «трусы Егорыча», «BMW M5 Competition»)
  → Вежливо объясни специализацию:
  «Мы специализируемся на технике Apple — MacBook и iMac 😊 Если интересует что-то из этой линейки, помогу!»
  НЕ говори «уточню у коллег» — это явно не наш товар.

═══ ЦВЕТ / КОНФИГУРАЦИЯ ═══
Когда покупатель спрашивает о цветах или конфигурациях:
• Все цвета и конфигурации доступны — не ограничивайся только каталогом.
• Уточни, что именно нужно: «А какой цвет и конфигурация вас интересуют?»
• Когда покупатель назовёт — подбери из каталога ближайший вариант и назови цену,
  либо скажи «Минуту, уточняю».

═══ ВЕДЕНИЕ К СДЕЛКЕ ═══
Цель каждого ответа — сдвинуть клиента на один шаг к покупке. Всегда заканчивай уточняющим вопросом или призывом к действию:

• Назвал цену → «Вас устраивает цена?» / «Готовы оформить?»
• Клиент говорит «устраивает» / «беру» → «Отлично! Когда удобно подъехать? Забронируем для вас 🎉»
• Клиент говорит «дорого» / «не устраивает» → «Что именно смущает — цена или что-то ещё?»
  Если цена — предложи скидку 500 ₽ (один раз за диалог, не больше):
  «Специально для вас — скидка 500 ₽, итого [цена − 500] ₽ 💰 Как вам?»
• После скидки клиент всё равно отказывается → передай торг менеджеру (см. ПЕРЕГОВОРЫ О ЦЕНЕ).
• Клиент спросил про товар и замолчал → «Остались вопросы или готовы оформить? 🙏»
• Не задавай два вопроса подряд — выбери один, самый важный для следующего шага.

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

═══ ТРЕЙД-ИН ═══
Если покупатель спрашивает про трейд-ин / обмен / сдать старое устройство:
→ «Да, принимаем в трейд-ин 😊 Оценку делает наш специалист на месте.»

═══ РАСХОЖДЕНИЕ ЦЕН ═══
Если цена в объявлении отличается от цены в каталоге:
→ «Да, в объявлении вышла ошибка с ценой — цена [X] ₽, уже исправляем. Скажите, устраивает вас эта цена?»
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

═══ АДРЕС И САМОВЫВОЗ ═══
Если покупатель спрашивает адрес / как пройти / где находитесь / самовывоз:
→ «Барклая 8, возле БЦ Рубин. Как подойдёте — позвоните по номеру 8 916 202-43-44, мы встретим или сориентируем 😊»

═══ КАК НАЗЫВАТЬ ЦЕНУ ═══
Называй цену просто числом с ₽. НЕЛЬЗЯ использовать слова «актуальная», «актуальный», «неактуальная» рядом с ценой — это снижает рейтинг объявления.
Правильно: «Цена — 101 000 ₽» или просто «101 000 ₽».
Неправильно: «Актуальная цена — 101 000 ₽», «актуальный прайс».

═══ КЛИЕНТ УХОДИТ / «НАДО ПОДУМАТЬ» ═══
Если покупатель говорит «надо подумать», «напишу позже», «в сентябре», «пока не готов», «позже», «не сейчас» или любой другой сигнал откладывания:
→ «Хорошо, ждём вас! 😊 А пока можете вступить в наш тг-канал Gulai_store — там следим за ценами и выкладываем новинки.»
ВАЖНО: упоминай только название Gulai_store, без ссылок, без @, без t.me — иначе Авито может заблокировать сообщение.

═══ ГАРАНТИЯ ═══
Если покупатель спрашивает про гарантию, условия, сервис:
→ «На все наши товары — гарантия 12 месяцев 😊»
Можно добавить, что выдаём кассовый чек.

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
        price_db: Optional[PriceDatabase] = None,
    ) -> None:
        self.db = db
        self.client = openai_client
        self.config = config
        self.stock_source = stock_source
        self.price_db = price_db  # optional live price override

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

    @staticmethod
    def _make_sku(item: dict) -> str:
        if "db_sku" in item:
            return item["db_sku"]
        import re as _re
        parts = [item.get("name", ""), item.get("config", ""), item.get("color", "")]
        raw = " ".join(p for p in parts if p).lower().strip()
        return _re.sub(r"[^a-zа-яёё0-9]+", "_", raw).strip("_")

    async def _format_catalog(self) -> str:
        cat_notes = self._cat.get("category_notes", {})
        lines: list[str] = []
        for category, items in self._cat.get("categories", {}).items():
            note = cat_notes.get(category, "")
            header = f"\n{category}" + (f" [{note}]" if note else "") + ":"
            lines.append(header)
            for item in items:
                name = item["name"]
                if item.get("config"):
                    name = f"{name} {item['config']}"
                if item.get("color"):
                    name = f"{name} ({item['color']})"

                if "markup" in item:
                    # New-style: final price = db_price + fixed markup
                    markup: int = item["markup"]
                    db_price: Optional[int] = None
                    sku = item.get("db_sku")
                    if self.price_db and sku:
                        try:
                            db_price = await self.price_db.get_price(sku)
                        except Exception:
                            logger.warning("price_db lookup failed for sku=%s", sku)
                    if db_price is not None:
                        price_str = f"{db_price + markup:,}".replace(",", " ")
                        lines.append(f"  • {name} — {price_str} ₽")
                    else:
                        lines.append(f"  • {name} — цена уточняется")
                else:
                    # Legacy-style: yaml price, optionally overridden by live price
                    yaml_price: int = item.get("price", 0)
                    live_price: Optional[int] = None
                    if self.price_db:
                        try:
                            live_price = await self.price_db.get_price(self._make_sku(item))
                        except Exception:
                            logger.warning("price_db lookup failed for %s", name)
                    final_price = live_price if live_price is not None else yaml_price
                    price_str = f"{final_price:,}".replace(",", " ")
                    lines.append(f"  • {name} — {price_str} ₽")
        return "\n".join(lines)

    async def _build_system_prompt(self) -> str:
        return _SYSTEM_PROMPT.format(
            knowledge_base=self._format_kb(),
            catalog=await self._format_catalog(),
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

        context = (
            f"Вопрос клиента: «{user_message[:300]}»\n"
            f"Ответ бота: «{bot_reply[:200]}»"
        )
        text = (
            f"📌 Эскалация в диалоге #{dialog_id}\n\n"
            f"Клиент: {dialog['external_id']}\n"
            f"Ссылка: {transport.get_dialog_link(dialog['external_id'])}\n\n"
            f"Вопрос клиента: «{user_message[:200]}»\n"
            f"Ответ бота: «{bot_reply[:200]}»"
        )
        tg_msg_id = await transport.send_owner_notification(text)
        await self.db.record_notification(dialog_id, "escalation",
                                          {"user_msg": user_message[:200]})
        if tg_msg_id is not None:
            await self.db.store_escalation_relay(
                tg_msg_id=tg_msg_id,
                dialog_id=dialog_id,
                transport=dialog["transport"],
                external_id=dialog["external_id"],
                context=context,
            )

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
        system_prompt = await self._build_system_prompt()
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

    async def reformulate_owner_reply(self, owner_text: str, context: str) -> str:
        """Переформулирует сырой ответ владельца в сообщение для покупателя."""
        prompt = (
            "Ты — помощница магазина Gulai Store. "
            "Владелец дал ответ на вопрос покупателя. "
            "Переформулируй его ответ в дружелюбное сообщение покупателю. "
            "Без вводных фраз, без Markdown, 1–3 предложения максимум. "
            "Обращайся на «вы».\n\n"
            f"Контекст:\n{context}\n\n"
            f"Ответ владельца: «{owner_text}»\n\n"
            "Сформулируй ответ покупателю:"
        )
        response = await self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()

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
