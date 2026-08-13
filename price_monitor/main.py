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


async def run_check(client: TelegramClient, db: PriceDatabase, cfg: Config) -> None:
    """One price-check cycle: fetch all sources, deduplicate, persist changes."""
    run_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("=== Price check at %s MSK ===", datetime.now(_MSK).strftime("%H:%M:%S"))

    # SKU -> (name, price, source, raw); always keep the CHEAPER price across sources
    seen: dict[str, tuple[str, int, str, str]] = {}

    group_prices = await fetch_group_prices(client, cfg.group_chat_id, cfg.group_msg_limit)
    for p in group_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "group", p.raw_line)

    bot_prices = await fetch_bot_prices(client, cfg.price_bot_username, cfg.bot_collect_timeout)
    for p in bot_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "bot", p.raw_line)

    # Track which sources returned data — prune_stale only removes entries from
    # active sources, preserving entries from offline/empty sources (e.g. bot after 19:00).
    sources_seen: set[str] = set()
    if group_prices:
        sources_seen.add("group")
    if bot_prices:
        sources_seen.add("bot")
    if not sources_seen:
        logger.warning("All sources returned 0 entries — skipping prune entirely")

    changed = 0
    for sku, (name, price, source, raw) in seen.items():
        if await db.upsert(sku, name, price, source, raw):
            changed += 1

    pruned = await db.prune_stale(run_started_at, sources_seen=sources_seen or None)
    logger.info("=== Done: %d/%d prices changed, %d stale removed ===", changed, len(seen), pruned)


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
