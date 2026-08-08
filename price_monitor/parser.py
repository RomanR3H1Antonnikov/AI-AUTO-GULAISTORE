"""
Price extraction from text.

TODO: adjust _PRICE_RE and parse_prices_from_text() once you share
the actual format of messages from the group / price bot.

Current assumption: lines like
  "MacBook Air 13 M3 256GB — 95 000 ₽"
  "MacBook Pro 14 M3 Pro — 155000"
"""

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ParsedPrice:
    sku: str        # stable key derived from name, used to match DB entries
    name: str       # human-readable name from message
    price: int      # price in rubles
    raw_line: str   # original text line (for debugging)


# Matches: <name> — <digits with optional spaces> [₽|руб]
# Adjust this regex to match the real message format.
_PRICE_RE = re.compile(
    r"^(?P<name>[А-Яа-яёЁA-Za-z0-9][^—\-\n]{3,60}?)\s*[—\-–]+\s*"
    r"(?P<price>[\d][\d\s\xa0]{1,9})\s*(?:₽|руб\.?)?$",
    re.UNICODE | re.MULTILINE,
)


def make_sku(text: str) -> str:
    """
    Derive a stable, lowercase SKU from a product name string.
    Must produce the same result for matching names in YAML and in messages.
    """
    text = text.lower().strip()
    text = re.sub(r"[^a-zа-яёё0-9]+", "_", text)
    text = text.strip("_")
    return text


def parse_prices_from_text(text: str) -> list[ParsedPrice]:
    """
    Extract all price entries from a block of text.
    Returns a (possibly empty) list of ParsedPrice objects.
    """
    results: list[ParsedPrice] = []
    for m in _PRICE_RE.finditer(text):
        name = m.group("name").strip()
        price_str = re.sub(r"[\s\xa0]", "", m.group("price"))
        try:
            price = int(price_str)
        except ValueError:
            logger.debug("Non-numeric price in: %r", m.group(0))
            continue
        if not (1_000 <= price <= 10_000_000):
            logger.debug("Price out of range (%d) in: %r", price, m.group(0))
            continue
        results.append(ParsedPrice(
            sku=make_sku(name),
            name=name,
            price=price,
            raw_line=m.group(0).strip(),
        ))
    logger.debug("parse_prices_from_text: found %d entries", len(results))
    return results
