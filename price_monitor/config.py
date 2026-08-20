import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── Telegram API (from my.telegram.org) ──────────────────────────────────
    api_id: int = int(os.environ.get("TG_API_ID", "0"))
    api_hash: str = os.environ.get("TG_API_HASH", "")
    phone: str = os.environ.get("TG_PHONE", "")

    # Session file — created once interactively, reused on every restart
    session_path: str = os.environ.get("TG_SESSION_PATH", "price_monitor/session")

    # ── Source 1: Group chat ──────────────────────────────────────────────────
    # Numeric chat ID (e.g. -100123456789). Get it by forwarding a message to
    # @userinfobot or checking the URL in Telegram Web.
    group_chat_id: int = int(os.environ.get("PRICE_GROUP_ID", "0"))
    group_msg_limit: int = int(os.environ.get("PRICE_GROUP_MSG_LIMIT", "30"))

    # ── Source 2: Price bot ───────────────────────────────────────────────────
    # Bot username without @, e.g. "some_price_bot"
    price_bot_username: str = os.environ.get("PRICE_BOT_USERNAME", "")
    # Seconds to wait for bot to finish sending all messages
    bot_collect_timeout: float = float(os.environ.get("PRICE_BOT_TIMEOUT", "10"))

    # ── Schedule ──────────────────────────────────────────────────────────────
    check_interval_hours: float = float(os.environ.get("PRICE_CHECK_INTERVAL_HOURS", "3"))

    # ── Shared price database ─────────────────────────────────────────────────
    db_path: str = os.environ.get("PRICE_DB_PATH", "prices.db")

    # ── Notifications (sent via Telegram bot, not userbot) ───────────────────
    bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owner_telegram_id: int = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
    developer_telegram_id: int = int(os.environ.get("DEVELOPER_TELEGRAM_ID", "0"))
