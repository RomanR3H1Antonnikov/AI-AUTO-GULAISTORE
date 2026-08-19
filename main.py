"""
Gulai Store AI assistant — entry point.

Modes:
  Telegram-only (default): set TELEGRAM_BOT_TOKEN + OWNER_TELEGRAM_ID.
  Dual-mode (Telegram + Avito): additionally set AVITO_CLIENT_ID,
    AVITO_CLIENT_SECRET, and AVITO_WEBHOOK_URL.

In dual-mode both transports share one DialogEngine and one Database.
Owner notifications from Avito dialogs are forwarded through the Telegram bot
so the store owner receives all alerts in one place.

Startup sequence (dual-mode):
  1. DB init
  2. Telegram bot setup
  3. Avito credentials verification (fails loudly if misconfigured)
  4. Avito webhook registration (idempotent, non-fatal on failure)
  5. asyncio.gather(telegram_polling, avito_webhook_server)
"""

import asyncio
import logging
import os

import uvicorn
import yaml
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.adapters.avito_adapter import AvitoTransport
from src.adapters.avito_api_client import AvitoApiClient
from src.adapters.avito_auth import AvitoAuthClient
from src.adapters.avito_setup import setup_avito_webhook, verify_avito_credentials
from src.adapters.avito_webhook_server import AvitoWebhookServer
from src.adapters.telegram_adapter import TelegramAdapter
from src.core.dialog_engine import DialogEngine
from src.core.stock_source import StubStockSource
from src.storage.database import Database
from src.storage.price_database import PriceDatabase

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gulaistore.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _run_telegram(dp: Dispatcher, bot: Bot) -> None:
    logger.info("Telegram polling started.")
    try:
        await dp.start_polling(bot, skip_updates=True, polling_timeout=3)
    finally:
        await bot.session.close()
        logger.info("Telegram polling stopped.")


_RETENTION_SKIP_PHRASES = (
    "хорошего дня",
    "хорошего вечера",
    "хорошего утра",
    "хорошей ночи",
    "всего доброго",
    "до свидания",
    "gulai_store",          # канал Gulai_store — конец диалога с приглашением в канал
    "уточню наличие",       # бот ждёт ответа владельца — retention не нужен
    "одну минуту",          # то же самое, более общий вариант фразы
    "сразу напишу",         # «как только появится информация, сразу напишу» — бот обещал написать первым
    "менеджер свяжется",    # запрос передан менеджеру — ждём его, retention не нужен
    "свяжется с вами",
    "передала заявку",
    "передала ваш запрос",
    "передал ваш запрос",
)


async def _run_retention(db, transports: dict, silence_minutes: int = 5, interval: int = 60) -> None:
    """Periodically send a retention message to dialogs silent for silence_minutes."""
    RETENTION_MSG = "Подскажите, пожалуйста, вопрос ещё актуален?"
    while True:
        await asyncio.sleep(interval)
        try:
            dialogs = await db.get_dialogs_for_retention(silence_minutes)
        except Exception:
            logger.exception("Retention: failed to query dialogs")
            continue
        for dialog in dialogs:
            transport = transports.get(dialog["transport"])
            if transport is None:
                continue

            # Skip retention if the last meaningful bot message was a goodbye or channel invite.
            # Walk backwards through recent messages to skip deleted/empty ones.
            try:
                recent = await db.get_messages(dialog["id"], limit=5)
                last_text = None
                for msg in reversed(recent):
                    text = (msg.get("text") or "").strip()
                    if msg["role"] == "assistant" and len(text) >= 3:
                        last_text = text.lower()
                        break
                if last_text and any(p in last_text for p in _RETENTION_SKIP_PHRASES):
                    logger.info(
                        "Retention skipped → dialog %d (last meaningful msg is closing/channel invite)",
                        dialog["id"],
                    )
                    await db.record_notification(dialog["id"], "retention", {})
                    continue
            except Exception:
                logger.exception("Retention: failed to check last message for dialog %d", dialog["id"])

            try:
                await transport.send_message(dialog["external_id"], RETENTION_MSG)
                await db.add_message(dialog["id"], "assistant", RETENTION_MSG)
                logger.info(
                    "Retention sent → dialog %d (%s/%s)",
                    dialog["id"], dialog["transport"], dialog["external_id"],
                )
            except Exception:
                logger.exception(
                    "Retention: failed to send to dialog %d (%s/%s) — marking notified to skip",
                    dialog["id"], dialog["transport"], dialog["external_id"],
                )
            # Always mark notified — prevents infinite retry on dead/invalid chats
            try:
                await db.record_notification(dialog["id"], "retention", {})
            except Exception:
                logger.exception("Retention: failed to record notification for dialog %d", dialog["id"])


async def _run_uvicorn(app, port: int) -> None:
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        loop="none",       # reuse the existing asyncio event loop
        log_config=None,   # keep our own logging config intact
    )
    server = uvicorn.Server(config)
    logger.info("Avito webhook server listening on 0.0.0.0:%d", port)
    await server.serve()
    logger.info("Avito webhook server stopped.")


async def main() -> None:
    config = _load_config()

    # ── Storage ───────────────────────────────────────────────────────────────
    db = Database(config.get("db_path", "gulaistore.db"))
    await db.init()

    # ── LLM client ────────────────────────────────────────────────────────────
    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    # ── Live price database (optional — activated when prices.db exists) ─────
    price_db_path = config.get("price_db_path", "prices.db")
    price_db: PriceDatabase | None = None
    import os as _os
    if _os.path.exists(price_db_path):
        price_db = PriceDatabase(price_db_path)
        await price_db.init()
        logger.info("Live price database loaded from %s", price_db_path)
    else:
        logger.info("prices.db not found — using YAML catalog prices (run price_monitor to enable live prices)")

    # ── Dialog engine (shared between all transports) ─────────────────────────
    engine = DialogEngine(
        db=db,
        openai_client=openai_client,
        knowledge_base_path="data/knowledge_base.yaml",
        catalog_path="data/catalog.yaml",
        config=config.get("engine", {}),
        stock_source=StubStockSource(),
        price_db=price_db,
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    bot = Bot(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        default=DefaultBotProperties(),
    )
    dp = Dispatcher()
    tg_adapter = TelegramAdapter(
        bot=bot,
        owner_telegram_id=int(os.environ["OWNER_TELEGRAM_ID"]),
        engine=engine,
        developer_telegram_id=int(os.environ.get("DEVELOPER_TELEGRAM_ID", "0")),
    )
    dp.include_router(tg_adapter.router)

    # Tasks to run concurrently. Telegram is always present.
    tasks: list[asyncio.Task] = [
        asyncio.create_task(_run_telegram(dp, bot), name="telegram"),
    ]

    # ── Avito (optional — enabled when all three env vars are set) ────────────
    avito_client_id     = os.environ.get("AVITO_CLIENT_ID", "").strip()
    avito_client_secret = os.environ.get("AVITO_CLIENT_SECRET", "").strip()
    avito_webhook_url   = os.environ.get("AVITO_WEBHOOK_URL", "").strip()
    avito_port          = int(os.environ.get("AVITO_WEBHOOK_PORT", "8080"))

    avito_api_client: AvitoApiClient | None = None

    if avito_client_id and avito_client_secret and avito_webhook_url:
        logger.info("Avito mode: initialising...")

        auth = AvitoAuthClient(avito_client_id, avito_client_secret)
        avito_api_client = AvitoApiClient(auth)
        await avito_api_client.start()

        # Verify credentials and get canonical user_id from the API.
        # Raises RuntimeError on bad credentials — intentional fail-fast.
        avito_user_id = await verify_avito_credentials(avito_api_client)

        avito_transport = AvitoTransport(
            api=avito_api_client,
            avito_user_id=avito_user_id,
            # Avito alerts arrive in the same Telegram chat as Telegram-dialog alerts.
            owner_notifier=tg_adapter.transport.send_owner_notification,
        )

        # Wire owner-reply relay: TelegramAdapter → AvitoTransport
        tg_adapter.avito_reply_sender = avito_transport.send_message

        # Register webhook — idempotent, non-fatal on failure.
        await setup_avito_webhook(avito_api_client, avito_webhook_url)

        webhook_server = AvitoWebhookServer(engine=engine, transport=avito_transport)
        tasks.append(
            asyncio.create_task(
                _run_uvicorn(webhook_server.app, avito_port), name="avito_webhook"
            )
        )
        tasks.append(
            asyncio.create_task(
                _run_retention(db, {"avito": avito_transport}), name="retention"
            )
        )
        logger.info(
            "Avito mode ready | webhook: %s → internal port %d",
            avito_webhook_url, avito_port,
        )
    else:
        logger.info(
            "Avito mode disabled (AVITO_CLIENT_ID / AVITO_CLIENT_SECRET / "
            "AVITO_WEBHOOK_URL not configured). Running Telegram-only."
        )

    logger.info("Gulai Store AI assistant started.")

    try:
        await asyncio.gather(*tasks)
    finally:
        await db.close()
        if price_db:
            await price_db.close()
        if avito_api_client:
            await avito_api_client.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
