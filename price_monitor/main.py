"""
Gulai Store — Price Monitor service.

Runs on VPS alongside the main autoresponder.
Every N hours fetches prices from two Telegram sources, detects changes,
and updates prices.db which the autoresponder reads for live prices.

First-time setup (interactive, run locally or via SSH):
  python -m price_monitor.main --auth

Normal operation (managed by systemd):
  python -m price_monitor.main
"""

import asyncio
import logging
import sys
from datetime import datetime

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


async def run_check(client: TelegramClient, db: PriceDatabase, cfg: Config) -> None:
    """One price-check cycle: fetch all sources, deduplicate, persist changes."""
    logger.info("=== Price check at %s ===", datetime.now().strftime("%H:%M:%S"))


    # SKU -> (name, price, source, raw); always keep the CHEAPER price across sources
    seen: dict[str, tuple[str, int, str, str]] = {}  # sku -> (name, price, source, raw)

    group_prices = await fetch_group_prices(client, cfg.group_chat_id, cfg.group_msg_limit)
    for p in group_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "group", p.raw_line)

    bot_prices = await fetch_bot_prices(client, cfg.price_bot_username, cfg.bot_collect_timeout)
    for p in bot_prices:
        if p.sku not in seen or p.price < seen[p.sku][1]:
            seen[p.sku] = (p.name, p.price, "bot", p.raw_line)
    changed = 0
    for sku, (name, price, source, raw) in seen.items():
        if await db.upsert(sku, name, price, source, raw):
            changed += 1

    logger.info("=== Done: %d/%d prices changed ===", changed, len(seen))


async def main_loop(cfg: Config) -> None:
    db = PriceDatabase(cfg.db_path)
    await db.init()

    async with TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash) as client:
        me = await client.get_me()
        logger.info("Connected as %s (@%s)", me.first_name, me.username)

        while True:
            try:
                await run_check(client, db, cfg)
            except Exception:
                logger.exception("Unhandled error in price check")

            interval = cfg.check_interval_hours * 3600
            logger.info("Sleeping %.1fh until next check", cfg.check_interval_hours)
            await asyncio.sleep(interval)

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
