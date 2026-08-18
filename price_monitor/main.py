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
import sys
from datetime import datetime, timezone, timedelta

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
    # All today's slots passed — first slot tomorrow.
    h, m = _RUN_TIMES[0]
    return (base + timedelta(days=1)).replace(hour=h, minute=m)


def _format_price(price: int) -> str:
    """Format price as '107 500 ₽'."""
    return f"{price:,}".replace(",", " ") + " ₽"


async def _send_owner_report(
    client: TelegramClient,
    owner_id: int,
    run_time: datetime,
    stats: dict,
    changed_items: list[dict],
    disappeared_items: list[dict],
    needs_check_items: list[dict],
) -> None:
    """Send a Telegram message to the owner with the price update report."""
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
        await client.send_message(owner_id, text)
        logger.info("Owner report sent to %d", owner_id)
    except Exception:
        logger.exception("Failed to send owner report to %d", owner_id)


async def run_check(client: TelegramClient, db: PriceDatabase, cfg: Config) -> None:
    """One price-check cycle: fetch all sources, deduplicate, persist changes."""
    run_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    run_time_msk = datetime.now(_MSK)
    logger.info("=== Price check at %s MSK ===", run_time_msk.strftime("%H:%M:%S"))

    # SKU -> (name, price, source, raw, needs_check); always keep the CHEAPER price across sources
    seen: dict[str, tuple[str, int, str, str, bool]] = {}

    group_prices = await fetch_group_prices(client, cfg.group_chat_id, cfg.group_msg_limit)
    for p in group_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "group", p.raw_line, p.needs_check)

    bot_prices = await fetch_bot_prices(client, cfg.price_bot_username, cfg.bot_collect_timeout)
    for p in bot_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "bot", p.raw_line, p.needs_check)

    # Track which sources returned data — prune_stale only removes entries from
    # active sources, preserving entries from offline/empty sources (e.g. bot after 19:00).
    sources_seen: set[str] = set()
    if group_prices:
        sources_seen.add("group")
    if bot_prices:
        sources_seen.add("bot")
    if not sources_seen:
        logger.warning("All sources returned 0 entries — skipping prune entirely")

    # Collect per-run stats and item lists for owner report.
    stats = {"new": 0, "restored": 0, "changed": 0, "unchanged": 0, "needs_check": 0}
    changed_items: list[dict] = []     # new / restored / changed prices
    needs_check_items: list[dict] = [] # 🏎️ items

    for sku, (name, price, source, raw, needs_check) in seen.items():
        available = 0 if needs_check else 1
        status = await db.upsert(sku, name, price, source, raw, available=available)
        stats[status] = stats.get(status, 0) + 1
        if needs_check:
            stats["needs_check"] += 1
            needs_check_items.append({"sku": sku, "name": name, "price": price})
        if status in ("new", "restored", "changed"):
            # Get final price (with markup) for the report.
            final_price, _ = await db.get_price_info(sku)
            changed_items.append({"sku": sku, "name": name, "price": price,
                                   "final_price": final_price, "status": status})

    disappeared_count, disappeared_items = await db.prune_stale(
        run_started_at, sources_seen=sources_seen or None
    )
    stats["disappeared"] = disappeared_count

    # Count total active entries for summary.
    all_entries = await db.get_all()
    stats["total"] = len(all_entries)

    logger.info(
        "=== Done: new=%d restored=%d changed=%d unchanged=%d disappeared=%d needs_check=%d total=%d ===",
        stats.get("new", 0), stats.get("restored", 0), stats.get("changed", 0),
        stats.get("unchanged", 0), disappeared_count, stats.get("needs_check", 0), stats["total"],
    )

    # Send report only if something actually changed.
    if any(stats.get(k) for k in ("new", "restored", "changed", "disappeared", "needs_check")):
        await _send_owner_report(
            client, cfg.owner_telegram_id, run_time_msk,
            stats, changed_items, disappeared_items, needs_check_items,
        )
    else:
        logger.info("No changes detected — skipping owner report")


async def main_loop(cfg: Config) -> None:
    db = PriceDatabase(cfg.db_path)
    await db.init()

    async with TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash) as client:
        me = await client.get_me()
        logger.info("Connected as %s (@%s)", me.first_name, me.username)

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
                await run_check(client, db, cfg)
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
