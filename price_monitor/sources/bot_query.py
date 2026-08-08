"""
Source 2 — Price bot query.

Sends /start to a configured bot and collects all response messages
within a timeout window, then parses prices from the combined text.
"""

import asyncio
import logging

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from price_monitor.parser import ParsedPrice, parse_prices_from_text

logger = logging.getLogger(__name__)


async def fetch_bot_prices(
    client: TelegramClient,
    bot_username: str,
    timeout: float = 10.0,
) -> list[ParsedPrice]:
    """
    Send /start to bot_username and collect all messages until `timeout` elapses.
    Returns parsed price entries from the combined response text.
    """
    if not bot_username:
        logger.warning("PRICE_BOT_USERNAME not set — skipping bot source")
        return []

    collected: list[str] = []
    try:
        async with client.conversation(bot_username, timeout=timeout) as conv:
            await conv.send_message("/start")
            logger.debug("Sent /start to %s", bot_username)
            while True:
                try:
                    msg = await conv.get_response(timeout=timeout)
                    if msg.text:
                        collected.append(msg.text)
                        logger.debug(
                            "Bot response chunk (%d chars)", len(msg.text)
                        )
                except asyncio.TimeoutError:
                    break
    except FloodWaitError as e:
        logger.warning("FloodWaitError from %s — wait %ds", bot_username, e.seconds)
    except Exception:
        logger.exception("Error querying bot %s", bot_username)

    combined = "\n".join(collected)
    prices = parse_prices_from_text(combined)
    logger.info(
        "Bot source: %d price entries from %d message(s)", len(prices), len(collected)
    )
    return prices
