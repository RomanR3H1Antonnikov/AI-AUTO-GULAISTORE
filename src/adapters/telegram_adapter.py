"""
Telegram adapter — Phase 1 transport implementation.

Architecture:
  - TelegramTransport  → implements core.Transport
  - TelegramAdapter    → wires aiogram handlers to DialogEngine

One bot, two roles:
  - Regular users  → treated as buyers, processed by DialogEngine
  - Owner (by ID)  → receives notifications, issues admin commands

Owner commands:
  /stop <chat_id>   — manual takeover (bot goes silent in that dialog)
  /start <chat_id>  — resume bot in that dialog
  /status <chat_id> — show dialog stats
  /dialogs          — list recent dialogs
"""

import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..core.dialog_engine import DialogEngine
from ..core.transport import IncomingMessage, Transport

logger = logging.getLogger(__name__)


class TelegramTransport(Transport):
    """Implements Transport for the Telegram platform."""

    def __init__(self, bot: Bot, owner_id: int) -> None:
        self._bot = bot
        self._owner_id = owner_id
        self._bot_id: Optional[int] = None

    @property
    def name(self) -> str:
        return "telegram"

    async def send_message(self, dialog_id: str, text: str) -> None:
        await self._bot.send_message(chat_id=int(dialog_id), text=text)

    async def send_owner_notification(self, text: str) -> Optional[int]:
        msg = await self._bot.send_message(chat_id=self._owner_id, text=text)
        return msg.message_id

    def get_dialog_link(self, dialog_id: str) -> str:
        return f"tg://user?id={dialog_id}"

    async def get_sender_name(self, sender_id: str) -> str:
        try:
            chat = await self._bot.get_chat(int(sender_id))
            return chat.full_name or chat.username or sender_id
        except Exception:
            return sender_id

    async def get_bot_id(self) -> int:
        if self._bot_id is None:
            me = await self._bot.get_me()
            self._bot_id = me.id
        return self._bot_id


class TelegramAdapter:
    """
    Bridges aiogram event handlers → DialogEngine.
    All business logic stays in the engine; the adapter only translates
    Telegram-specific types to/from the platform-agnostic interfaces.
    """

    def __init__(self, bot: Bot, owner_telegram_id: int, engine: DialogEngine) -> None:
        self.engine = engine
        self.owner_id = owner_telegram_id
        self.transport = TelegramTransport(bot, owner_telegram_id)
        self.router = Router(name="gulaistore")
        # Injected by main.py after Avito transport is created
        self.avito_reply_sender: Optional[callable] = None
        self._register_handlers()

    def _is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_id

    def _register_handlers(self) -> None:
        r = self.router

        # ── Owner admin commands ──────────────────────────────────────────────
        r.message(Command("stop"),    F.from_user.id == self.owner_id)(self._cmd_stop)
        r.message(Command("status"),  F.from_user.id == self.owner_id)(self._cmd_status)
        r.message(Command("dialogs"), F.from_user.id == self.owner_id)(self._cmd_dialogs)

        # /start with args = resume a dialog; without args = owner opens the bot
        r.message(Command("start"),   F.from_user.id == self.owner_id)(self._cmd_start_owner)

        # Owner non-command messages (handles escalation relay; ignores everything else)
        r.message(F.from_user.id == self.owner_id)(self._handle_owner_message)

        # ── Buyer /start ──────────────────────────────────────────────────────
        r.message(Command("start"))(self._cmd_start_buyer)

        # ── All other messages ────────────────────────────────────────────────
        r.message()(self._handle_message)

    # ── Owner commands ────────────────────────────────────────────────────────

    async def _cmd_start_owner(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer(
                "👋 Панель управления Gulai Store AI\n\n"
                "Команды:\n"
                "  /stop CHAT_ID — остановить бота в диалоге\n"
                "  /start CHAT_ID — возобновить бота\n"
                "  /status CHAT_ID — статус диалога\n"
                "  /dialogs — список диалогов",
                parse_mode=None,
            )
            return

        external_id = command.args.strip()
        ok = await self.engine.handle_resume("telegram", external_id)
        if ok:
            await message.answer(f"✅ Бот возобновлён в диалоге {external_id}.")
            logger.info("owner resumed dialog %s", external_id)
        else:
            await message.answer(f"❌ Диалог {external_id} не найден.")

    async def _cmd_stop(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /stop <chat_id>")
            return

        external_id = command.args.strip()
        ok = await self.engine.handle_takeover("telegram", external_id, manual=True)
        if ok:
            await message.answer(f"✅ Бот остановлен в диалоге {external_id} (ручной takeover).")
            logger.info("manual takeover by owner: dialog %s", external_id)
        else:
            await message.answer(f"❌ Диалог {external_id} не найден.")

    async def _cmd_status(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /status <chat_id>")
            return

        external_id = command.args.strip()
        dialog = await self.engine.db.get_dialog("telegram", external_id)
        if not dialog:
            await message.answer(f"Диалог {external_id} не найден.")
            return

        tokens = await self.engine.db.get_daily_tokens(dialog["id"])
        msg_count = await self.engine.db.get_message_count(dialog["id"])
        status_emoji = {"bot_active": "🟢", "owner_takeover": "🔴", "silenced": "⚫"}.get(
            dialog["status"], "❓"
        )
        await message.answer(
            f"{status_emoji} Диалог #{dialog['id']}\n"
            f"Клиент: {external_id}\n"
            f"Статус: {dialog['status']}\n"
            f"Takeover: {dialog.get('takeover_type') or '—'}\n"
            f"Сообщений: {msg_count}\n"
            f"Токенов сегодня: {tokens:,}\n"
            f"Создан: {dialog['created_at']}\n"
            f"Обновлён: {dialog['updated_at']}"
        )

    async def _cmd_dialogs(self, message: Message) -> None:
        dialogs = await self.engine.db.list_dialogs(limit=10)
        if not dialogs:
            await message.answer("Нет диалогов.")
            return

        emoji = {"bot_active": "🟢", "owner_takeover": "🔴", "silenced": "⚫"}
        lines = ["📋 Последние диалоги:\n"]
        for d in dialogs:
            e = emoji.get(d["status"], "❓")
            lines.append(f"{e} #{d['id']} | {d['external_id']} | {d['status']}")
            lines.append(f"   {d['updated_at']}")
        await message.answer("\n".join(lines))

    # ── Buyer /start ──────────────────────────────────────────────────────────

    async def _cmd_start_buyer(self, message: Message) -> None:
        incoming = IncomingMessage(
            dialog_id=str(message.chat.id),
            sender_id=str(message.from_user.id),
            text="Здравствуйте",
            is_owner_message=False,
            transport_name="telegram",
        )
        reply = await self.engine.process_message(self.transport, incoming)
        if reply:
            await message.answer(reply)

    # ── Owner non-command messages ────────────────────────────────────────────

    async def _handle_owner_message(self, message: Message) -> None:
        """
        Owner replied to an escalation notification → relay their answer to Avito.
        Plain (non-reply) messages from owner are silently ignored.
        """
        if message.reply_to_message is None:
            logger.debug("ignoring non-reply message from owner")
            return

        replied_msg_id = message.reply_to_message.message_id
        relay = await self.engine.db.get_escalation_relay(replied_msg_id)
        if relay is None:
            # Owner replied to a lead/toxicity notification, not an escalation
            logger.debug("owner replied to non-escalation tg_msg_id=%d — ignored", replied_msg_id)
            return

        if not message.text:
            await message.answer("Пришли текстом — перешлю клиенту.")
            return

        try:
            reformulated = await self.engine.reformulate_owner_reply(
                owner_text=message.text,
                context=relay["context"],
            )
        except Exception as exc:
            logger.error("reformulate_owner_reply failed: %s", exc)
            await message.answer(f"Ошибка при обработке ответа: {exc}")
            return

        if self.avito_reply_sender is not None:
            try:
                await self.avito_reply_sender(relay["external_id"], reformulated)
                await self.engine.db.add_message(relay["dialog_id"], "assistant", reformulated)
                await self.engine.db.delete_escalation_relay(replied_msg_id)
                await message.answer(f"Отправлено клиенту:\n\n{reformulated}")
                logger.info(
                    "owner reply relayed to Avito chat %s: %r",
                    relay["external_id"], reformulated[:80],
                )
            except Exception as exc:
                logger.error("Failed to relay to Avito chat %s: %s", relay["external_id"], exc)
                await message.answer(f"Ошибка отправки в Авито: {exc}\n\nГотовый ответ:\n{reformulated}")
        else:
            await message.answer(
                f"Avito не подключён — не могу переслать.\n\nГотовый ответ:\n{reformulated}"
            )

    # ── Generic message handler ───────────────────────────────────────────────

    async def _handle_message(self, message: Message) -> None:
        sender_id = message.from_user.id if message.from_user else None
        if sender_id is None:
            return

        # Loop protection: ignore own messages
        bot_id = await self.transport.get_bot_id()
        if sender_id == bot_id:
            logger.debug("ignoring own message (loop protection)")
            return

        text = message.text or message.caption or ""
        if not text:
            return

        incoming = IncomingMessage(
            dialog_id=str(message.chat.id),
            sender_id=str(sender_id),
            text=text,
            is_owner_message=False,
            transport_name="telegram",
        )
        reply = await self.engine.process_message(self.transport, incoming)
        if reply:
            await message.answer(reply)
