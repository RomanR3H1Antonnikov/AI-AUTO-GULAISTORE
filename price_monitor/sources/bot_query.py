"""
Source 2 — Price bot query.

Sends /start to a configured bot, then presses the reply-keyboard button
"Полный прайс-лист" and collects all response messages for parsing.
"""

import asyncio
import logging

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from price_monitor.parser import ParsedPrice, parse_prices_from_text

logger = logging.getLogger(__name__)

_FULL_PRICE_BUTTON = "Полный прайс-лист"


async def fetch_bot_prices(
    client: TelegramClient,
    bot_username: str,
    timeout: float = 15.0,
) -> list[ParsedPrice]:
    """
    Send /start, wait for the reply keyboard, then send the "Полный прайс-лист"
    button text and collect all response messages until timeout elapses.
    """
    if not bot_username:
        logger.warning("PRICE_BOT_USERNAME not set — skipping bot source")
        return []

    collected: list[str] = []
    try:
        async with client.conversation(bot_username, timeout=timeout) as conv:
            await conv.send_message("/start")
            logger.debug("Sent /start to %s", bot_username)

            # Wait for the bot's greeting / keyboard message
            await conv.get_response(timeout=timeout)

            # Press the reply-keyboard button by sending its text
            await conv.send_message(_FULL_PRICE_BUTTON)
            logger.debug("Sent button text: %s", _FULL_PRICE_BUTTON)

            # Collect all price messages until the bot stops responding
            while True:
                try:
                    msg = await conv.get_response(timeout=timeout)
                    if msg.text:
                        collected.append(msg.text)
                        logger.debug("Bot response chunk (%d chars)", len(msg.text))
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
