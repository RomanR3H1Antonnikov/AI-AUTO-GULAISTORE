"""
Source 1 — Telegram group monitor.

Reads the last N messages from a specified group/channel.
Handles edited messages automatically (Telethon always returns the current version).
"""

import logging
from telethon import TelegramClient

from price_monitor.parser import ParsedPrice, parse_prices_from_text

logger = logging.getLogger(__name__)


async def fetch_group_prices(
    client: TelegramClient,
    chat_id: int,
    limit: int = 30,
) -> list[ParsedPrice]:
    """
    Iterate the last `limit` messages in `chat_id` and extract price entries.
    Returns an empty list if chat_id is 0 or if no prices found.
    """
    if not chat_id:
        logger.warning("PRICE_GROUP_ID not set — skipping group source")
        return []

    all_prices: list[ParsedPrice] = []
    try:
        async for message in client.iter_messages(chat_id, limit=limit):
            if not message.text:
                continue
            prices = parse_prices_from_text(message.text)
            if prices:
                logger.debug(
                    "Group msg id=%d: extracted %d price(s)", message.id, len(prices)
                )
                all_prices.extend(prices)
    except Exception:
        logger.exception("Error reading group %s", chat_id)

    logger.info("Group source: %d price entries total", len(all_prices))
    return all_prices
