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
import sys
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telethon import TelegramClient, events

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

# Message IDs of price reports sent to the owner — replies to these trigger price queries.
_report_msg_ids: set[int] = set()


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


# ── Owner price query via LLM ─────────────────────────────────────────────────

async def _handle_owner_price_query(event, db: PriceDatabase, openai: AsyncOpenAI) -> None:
    """Owner replied to a price report — treat it as a DB price query."""
    question = (event.raw_text or "").strip()
    if not question:
        return

    logger.info("Owner price query: %r", question[:120])

    results = await db.search_prices(question, limit=15)
    if not results:
        await event.reply("Ничего не найдено по этому запросу в базе цен.")
        return

    lines: list[str] = []
    for r in results:
        status = "в наличии" if r["available"] else "❌ нет в прайсе (пропало)"
        markup = r.get("markup_pct") or 0
        raw = r["price"]
        final = round(raw * (1 + markup / 100))
        changed = r.get("price_changed_at") or r.get("updated_at") or "неизвестно"
        # Trim seconds from timestamp if present
        if isinstance(changed, str) and len(changed) > 16:
            changed = changed[:16]
        lines.append(
            f"• {r['name']}\n"
            f"  SKU: {r['sku']}\n"
            f"  Закупочная: {_format_price(raw)}"
            + (f"  Наценка: {markup}% → итог: {_format_price(final)}" if markup else "") + "\n"
            f"  Статус: {status}\n"
            f"  Изменено/пропало: {changed}"
        )

    context = "\n\n".join(lines)
    prompt = (
        "Ты — аналитик магазина Gulai Store, отвечаешь владельцу на вопрос о ценах.\n"
        "Используй ТОЛЬКО данные из базы ниже. Отвечай кратко, по-русски.\n"
        "Если несколько подходящих позиций — перечисли все.\n"
        "Формат: название, закупочная, наценка (если есть), итоговая, статус, дата изменения.\n\n"
        f"Вопрос: {question}\n\n"
        f"Данные из базы:\n{context}"
    )

    try:
        resp = await openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("LLM price query failed: %s", exc)
        answer = f"Ошибка при обращении к LLM: {exc}\n\nРезультаты поиска:\n{context}"

    await event.reply(answer)
    logger.info("Answered owner price query for %r", question[:60])


# ── Owner report ──────────────────────────────────────────────────────────────

async def _send_owner_report(
    client: TelegramClient,
    owner_id: int,
    developer_id: int,
    run_time: datetime,
    stats: dict,
    changed_items: list[dict],
    disappeared_items: list[dict],
    needs_check_items: list[dict],
) -> int | None:
    """Send price update report to owner (and read-only copy to developer).

    Returns the owner's message ID so replies can be tracked for price queries.
    Developer gets a copy but their replies are intentionally ignored.
    """
    if not owner_id:
        logger.warning("OWNER_TELEGRAM_ID not set — skipping owner report")
        return None

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

    if changed_items:
        lines.append("\n— Изменения цен —")
        for item in changed_items:
            price_str = _format_price(item["final_price"]) if item.get("final_price") else "?"
            symbol = "🆕" if item["status"] == "new" else ("🔄" if item["status"] == "restored" else "💰")
            lines.append(f"{symbol} {item['name']}: {price_str}")

    if needs_check_items:
        lines.append("\n— Уточнить у владельца (🏎) —")
        for item in needs_check_items:
            lines.append(f"🏎 {item['name']}")

    if disappeared_items:
        lines.append("\n— Пропали из прайса —")
        for item in disappeared_items:
            lines.append(f"❌ {item['name']}")

    text = "\n".join(lines)
    try:
        msg = await client.send_message(owner_id, text)
        logger.info("Owner report sent to %d (msg_id=%d)", owner_id, msg.id)
    except Exception:
        logger.exception("Failed to send owner report to %d", owner_id)
        return None

    if developer_id:
        try:
            dev_text = text.replace(
                "💬 Ответь на это сообщение любым вопросом о ценах — я проверю базу.",
                "ℹ️ Только для чтения — вопросы о ценах задаёт владелец.",
            )
            await client.send_message(developer_id, dev_text)
            logger.info("Developer report copy sent to %d", developer_id)
        except Exception:
            logger.warning("Failed to send report copy to developer %d", developer_id)

    return msg.id


# ── Price check cycle ─────────────────────────────────────────────────────────

async def run_check(client: TelegramClient, db: PriceDatabase, cfg: Config, openai_client: AsyncOpenAI) -> None:
    """One price-check cycle: fetch all sources, deduplicate, persist changes."""
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
                                   "final_price": final_price, "status": status})

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
        msg_id = await _send_owner_report(
            client, cfg.owner_telegram_id, cfg.developer_telegram_id, run_time_msk,
            stats, changed_items, disappeared_items, needs_check_items,
        )
        if msg_id:
            _report_msg_ids.add(msg_id)
            logger.info("Report msg_id %d registered for price queries", msg_id)
    else:
        logger.info("No changes detected — skipping owner report")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop(cfg: Config) -> None:
    db = PriceDatabase(cfg.db_path)
    await db.init()

    openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    async with TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash) as client:
        me = await client.get_me()
        logger.info("Connected as %s (@%s)", me.first_name, me.username)

        @client.on(events.NewMessage(from_users=cfg.owner_telegram_id))
        async def on_owner_message(event):
            if not event.is_reply:
                return
            if event.reply_to_msg_id not in _report_msg_ids:
                return
            try:
                await _handle_owner_price_query(event, db, openai_client)
            except Exception:
                logger.exception("Error handling owner price query")

        while True:
            now = datetime.now(_MSK)
            next_run = _next_run(now)
            wait = (next_run - now).total_seconds()
            logger.info(
                "Next price check at %s MSK (in %.1fh).",
                next_run.strftime("%H:%M"), wait / 3600,
            )
            await asyncio.sleep(wait)

            try:
                await run_check(client, db, cfg, openai_client)
            except Exception:
                logger.exception("Unhandled error in price check")

    await db.close()


async def auth_and_exit(cfg: Config) -> None:
    """Interactive one-time authentication — creates the .session file."""
    async with TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash) as client:
        await client.start(phone=cfg.phone)
        me = await client.get_me()
        logger.info("Authenticated as: %s (@%s)", me.first_name, me.username)
        logger.info("Session saved → %s.session", cfg.session_path)


if __name__ == "__main__":
    cfg = Config()

    if not cfg.api_id or not cfg.api_hash:
        logger.error("TG_API_ID and TG_API_HASH must be set in .env")
        sys.exit(1)

    if "--auth" in sys.argv:
        asyncio.run(auth_and_exit(cfg))
    else:
        asyncio.run(main_loop(cfg))
