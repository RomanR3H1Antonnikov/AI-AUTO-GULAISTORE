"""
Gulai Store — Price Monitor service.

Runs on VPS alongside the main autoresponder.
Fetches prices from two Telegram sources twice a day (10:30 and 18:00 MSK),
and updates prices.db which the autoresponder reads for live prices.

First-time setup (interactive, run locally or via SSH):
  python -m price_monitor.main --auth

Normal operation (managed by systemd):
  python -m price_monitor.main
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import aiosqlite

from aiogram import Bot
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

from price_monitor.config import Config
from price_monitor.sources.group_monitor import fetch_group_prices
from price_monitor.sources.bot_query import fetch_bot_prices
from src.storage.price_database import PriceDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("price_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))

# Two fixed runs per day (MSK): morning and evening.
_RUN_TIMES: list[tuple[int, int]] = [(10, 30), (18, 0)]


def _next_run(now: datetime) -> datetime:
    """Return the next scheduled run datetime in MSK."""
    base = now.replace(second=0, microsecond=0)
    for h, m in _RUN_TIMES:
        candidate = base.replace(hour=h, minute=m)
        if candidate > now:
            return candidate
    h, m = _RUN_TIMES[0]
    return (base + timedelta(days=1)).replace(hour=h, minute=m)


def _format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " ₽"


# ── Owner report ──────────────────────────────────────────────────────────────

async def _send_owner_report(
    bot: Bot,
    db: PriceDatabase,
    owner_id: int,
    developer_id: int,
    run_time: datetime,
    stats: dict,
    changed_items: list[dict],
    disappeared_items: list[dict],
    needs_check_items: list[dict],
) -> None:
    """Send price update report via the Telegram bot (same channel as escalations).

    Saves the message ID to prices.db so the main bot can handle owner price queries
    when the owner replies to the report.
    """
    if not owner_id:
        logger.warning("OWNER_TELEGRAM_ID not set — skipping owner report")
        return

    ts = run_time.strftime("%d %b, %H:%M МСК").lstrip("0")
    total = stats.get("total", 0)

    lines = [f"📊 Прайс обновлён — {ts}\n"]
    lines.append(f"Всего в базе: {total}")
    if stats.get("new"):
        lines.append(f"🆕 Новые: {stats['new']}")
    if stats.get("restored"):
        lines.append(f"🔄 Вернулись: {stats['restored']}")
    if stats.get("changed"):
        lines.append(f"💰 Изменились: {stats['changed']}")
    if stats.get("disappeared"):
        lines.append(f"❌ Пропали: {stats['disappeared']}")
    if stats.get("needs_check"):
        lines.append(f"🏎 Уточнить у владельца: {stats['needs_check']}")
    lines.append("\n💬 Ответь на это сообщение любым вопросом о ценах — я проверю базу.")

    _MAX_LIST = 20  # max items per section to stay under Telegram's 4096-char limit

    if changed_items:
        lines.append("\n— Изменения цен —")
        for item in changed_items[:_MAX_LIST]:
            price_str = _format_price(item["final_price"]) if item.get("final_price") else "?"
            symbol = "🆕" if item["status"] == "new" else ("🔄" if item["status"] == "restored" else "💰")
            lines.append(f"{symbol} {item['name']}: {price_str}")
        if len(changed_items) > _MAX_LIST:
            lines.append(f"… и ещё {len(changed_items) - _MAX_LIST} позиций")

    if needs_check_items:
        lines.append("\n— Уточнить у владельца (🏎) —")
        for item in needs_check_items:
            lines.append(f"🏎 {item['name']}")

    if disappeared_items:
        lines.append("\n— Пропали из прайса —")
        for item in disappeared_items[:_MAX_LIST]:
            lines.append(f"❌ {item['name']}")
        if len(disappeared_items) > _MAX_LIST:
            lines.append(f"… и ещё {len(disappeared_items) - _MAX_LIST} позиций")

    text = "\n".join(lines)
    try:
        msg = await bot.send_message(chat_id=owner_id, text=text)
        await db.save_report_msg_id(msg.message_id)
        logger.info("Owner report sent via bot to %d (msg_id=%d)", owner_id, msg.message_id)
    except Exception:
        logger.exception("Failed to send owner report to %d", owner_id)
        return

    if developer_id:
        try:
            dev_text = text.replace(
                "💬 Ответь на это сообщение любым вопросом о ценах — я проверю базу.",
                "ℹ️ Только для чтения — вопросы о ценах задаёт владелец.",
            )
            await bot.send_message(chat_id=developer_id, text=dev_text)
            logger.info("Developer report copy sent to %d", developer_id)
        except Exception:
            logger.warning("Failed to send report copy to developer %d", developer_id)


# ── Proactive Avito notifications ────────────────────────────────────────────

_KW_RE = re.compile(r'[a-zа-яё]{3,}|\d{3,4}', re.IGNORECASE | re.UNICODE)
_MIN_KW_OVERLAP = 2


def _extract_kw(text: str) -> set[str]:
    return set(_KW_RE.findall(text.lower()))


async def _notify_price_changes_to_avito_dialogs(
    changed_items: list[dict],
    cfg: Config,
    run_time_msk: datetime,
) -> None:
    """
    After a price update, proactively message active Avito dialogs where the
    specific products discussed had price changes. Sends only the relevant
    positions (matched by keyword overlap), not a generic broadcast.
    """
    # Only items the bot can actually quote (skip needs_check anomalies)
    quotable = [i for i in changed_items if not i.get("needs_check") and i.get("final_price")]
    if not quotable:
        return

    if not cfg.avito_client_id or not cfg.avito_client_secret or not cfg.avito_user_id:
        logger.info("AVITO_CLIENT_ID/SECRET/USER_ID not set — skipping proactive notifications")
        return

    # Cutoff: today 09:30 MSK (= 06:30 UTC)
    today = run_time_msk.date()
    cutoff_msk = datetime(today.year, today.month, today.day, 9, 30, tzinfo=_MSK)
    cutoff_utc = cutoff_msk.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    item_kw_pairs = [(item, _extract_kw(item["name"])) for item in quotable]

    conn = None
    api = None
    try:
        conn = await aiosqlite.connect(cfg.gulai_db_path)
        conn.row_factory = aiosqlite.Row

        # All Avito dialogs where the bot replied since 09:30 MSK
        async with conn.execute("""
            SELECT DISTINCT d.id, d.external_id
            FROM dialogs d
            JOIN messages m ON m.dialog_id = d.id
            WHERE d.transport = 'avito'
              AND NOT (d.status = 'owner_takeover' AND d.takeover_type = 'checkmark_silence')
              AND m.role = 'assistant'
              AND m.ts >= ?
        """, (cutoff_utc,)) as cur:
            dialogs = [dict(r) async for r in cur]

        if not dialogs:
            logger.info("No active Avito dialogs since 09:30 MSK — no proactive notifications")
            return

        logger.info("Checking %d Avito dialogs for price-change relevance", len(dialogs))
        notified = 0

        for dialog in dialogs:
            dialog_id = dialog["id"]

            # One notification per dialog per day
            async with conn.execute("""
                SELECT 1 FROM notifications
                WHERE dialog_id = ? AND type = 'price_update' AND sent_at >= ?
            """, (dialog_id, cutoff_utc)) as cur:
                if await cur.fetchone():
                    continue

            # Messages from today's session (user + assistant for keywords)
            async with conn.execute("""
                SELECT text FROM messages
                WHERE dialog_id = ? AND ts >= ?
                ORDER BY ts DESC LIMIT 20
            """, (dialog_id, cutoff_utc)) as cur:
                msgs = [row[0] async for row in cur]

            if not msgs:
                continue

            dialog_kw = _extract_kw(" ".join(msgs))
            relevant = [
                item for item, kw in item_kw_pairs
                if len(kw & dialog_kw) >= _MIN_KW_OVERLAP
            ]
            if not relevant:
                continue

            # Build targeted message with only matched positions
            lines = ["Хочу уточнить — цены обновились!"]
            lines.append("")
            for item in relevant[:8]:
                price_str = f"{item['final_price']:,}".replace(",", " ")
                lines.append(f"• {item['name']} — {price_str} ₽")
            lines.append("")
            lines.append("Если остались вопросы — пишите, всё расскажу!")
            text = "\n".join(lines)

            # Lazy init — only if there's something to actually send
            if api is None:
                from src.adapters.avito_auth import AvitoAuthClient
                from src.adapters.avito_api_client import AvitoApiClient
                auth = AvitoAuthClient(cfg.avito_client_id, cfg.avito_client_secret)
                api = AvitoApiClient(auth)
                await api.start()

            try:
                await api.send_message(cfg.avito_user_id, dialog["external_id"], text)
                await conn.execute(
                    "INSERT INTO notifications (dialog_id, type) VALUES (?,?)",
                    (dialog_id, "price_update"),
                )
                await conn.commit()
                logger.info(
                    "Price update sent to Avito dialog %d (%s) — %d item(s) matched",
                    dialog_id, dialog["external_id"], len(relevant),
                )
                notified += 1
            except Exception as exc:
                logger.warning(
                    "Failed to send price update to Avito dialog %s: %s",
                    dialog["external_id"], exc,
                )

        logger.info("Proactive price notifications done: %d sent", notified)

    except Exception:
        logger.exception("Error in _notify_price_changes_to_avito_dialogs")
    finally:
        if api is not None:
            await api.close()
        if conn is not None:
            await conn.close()


# ── Price check cycle ─────────────────────────────────────────────────────────

async def run_check(client: TelegramClient, bot: Bot, db: PriceDatabase, cfg: Config) -> int:
    """One price-check cycle: fetch all sources, deduplicate, persist changes.

    Returns the number of entries seen across all sources (0 = nothing fetched).
    """
    run_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    run_time_msk = datetime.now(_MSK)
    logger.info("=== Price check at %s MSK ===", run_time_msk.strftime("%H:%M:%S"))

    seen: dict[str, tuple[str, int, str, str, bool]] = {}

    group_prices = await fetch_group_prices(client, cfg.group_chat_id, cfg.group_msg_limit)
    for p in group_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "group", p.raw_line, p.needs_check)

    bot_prices = await fetch_bot_prices(client, cfg.price_bot_username, cfg.bot_collect_timeout)
    for p in bot_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "bot", p.raw_line, p.needs_check)

    sources_seen: set[str] = set()
    if group_prices:
        sources_seen.add("group")
    if bot_prices:
        sources_seen.add("bot")
    if not sources_seen:
        logger.warning("All sources returned 0 entries — skipping prune entirely")
        return 0

    stats = {"new": 0, "restored": 0, "changed": 0, "unchanged": 0, "needs_check": 0}
    changed_items: list[dict] = []
    needs_check_items: list[dict] = []

    for sku, (name, price, source, raw, needs_check) in seen.items():
        available = 0 if needs_check else 1
        status = await db.upsert(sku, name, price, source, raw, available=available)
        stats[status] = stats.get(status, 0) + 1
        if needs_check:
            stats["needs_check"] += 1
            needs_check_items.append({"sku": sku, "name": name, "price": price})
        if status in ("new", "restored", "changed"):
            final_price, _ = await db.get_price_info(sku)
            changed_items.append({"sku": sku, "name": name, "price": price,
                                   "final_price": final_price, "status": status,
                                   "needs_check": needs_check})

    disappeared_count, disappeared_items = await db.prune_stale(
        run_started_at, sources_seen=sources_seen or None
    )
    stats["disappeared"] = disappeared_count

    all_entries = await db.get_all()
    stats["total"] = len(all_entries)

    logger.info(
        "=== Done: new=%d restored=%d changed=%d unchanged=%d disappeared=%d needs_check=%d total=%d ===",
        stats.get("new", 0), stats.get("restored", 0), stats.get("changed", 0),
        stats.get("unchanged", 0), disappeared_count, stats.get("needs_check", 0), stats["total"],
    )

    if any(stats.get(k) for k in ("new", "restored", "changed", "disappeared", "needs_check")):
        await _send_owner_report(
            bot, db, cfg.owner_telegram_id, cfg.developer_telegram_id, run_time_msk,
            stats, changed_items, disappeared_items, needs_check_items,
        )
    else:
        logger.info("No changes detected — skipping owner report")

    if changed_items:
        await _notify_price_changes_to_avito_dialogs(changed_items, cfg, run_time_msk)

    return len(seen)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop(cfg: Config) -> None:
    if not cfg.bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot send reports")
        sys.exit(1)

    db = PriceDatabase(cfg.db_path)
    await db.init()

    bot = Bot(token=cfg.bot_token)

    try:
        while True:
            now = datetime.now(_MSK)
            next_run = _next_run(now)
            wait = (next_run - now).total_seconds()
            logger.info(
                "Next price check at %s MSK (in %.1fh).",
                next_run.strftime("%H:%M"), wait / 3600,
            )
            await asyncio.sleep(wait)

            _RETRY_DELAY = 15 * 60  # seconds between retries on empty result
            _MAX_RETRIES = 3
            for attempt in range(1 + _MAX_RETRIES):
                client = TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        logger.error(
                            "Telethon session expired — run: python -m price_monitor.main --auth"
                        )
                        break
                    entries = await run_check(client, bot, db, cfg)
                except Exception:
                    logger.exception("Unhandled error in price check (attempt %d)", attempt + 1)
                    entries = -1  # treat errors as "nothing fetched"
                finally:
                    await client.disconnect()

                if entries != 0:
                    break  # got data (or error on first attempt) — no retry needed
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "All sources returned 0 — retry %d/%d in %d min",
                        attempt + 1, _MAX_RETRIES, _RETRY_DELAY // 60,
                    )
                    await asyncio.sleep(_RETRY_DELAY)
                else:
                    logger.error(
                        "All %d retries exhausted — prices unchanged until next scheduled run",
                        _MAX_RETRIES,
                    )
    finally:
        await bot.session.close()
        await db.close()


async def auth_and_exit(cfg: Config) -> None:
    """Interactive one-time authentication — creates the .session file."""
    import getpass
    client = TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)
    await client.start(
        phone=cfg.phone,
        password=lambda: getpass.getpass('Enter your Telegram 2FA Cloud Password: '),
    )
    me = await client.get_me()
    logger.info("Authenticated as: %s (@%s)", me.first_name, me.username)
    logger.info("Session saved → %s.session", cfg.session_path)
    await client.disconnect()


if __name__ == "__main__":
    cfg = Config()

    if not cfg.api_id or not cfg.api_hash:
        logger.error("TG_API_ID and TG_API_HASH must be set in .env")
        sys.exit(1)

    if "--auth" in sys.argv:
        asyncio.run(auth_and_exit(cfg))
    else:
        asyncio.run(main_loop(cfg))
