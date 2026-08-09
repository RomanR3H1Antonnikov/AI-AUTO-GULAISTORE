"""
Price extraction from Telegram group messages and price bot replies.

Supported formats:

Channel (em-dash separator, bare integer price):
  💻[MODEL_CODE] Product Name (Config) Color🇺🇸🇭🇰 — 58000
  iPad Product Name Config Color🇮🇳 — 58000
  `🎧AirPods Product — 16300`
  .iPad Product — 58000

Bot (hyphen separator, dotted price with ₽):
  Product Name Color 🇺🇸 eSim - 99.200₽
  Product Name (condition note) - 13.100₽🏎️

Lines not matching either pattern are silently skipped.
"""

import re
import unicodedata
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_EM_DASH = "—"  # U+2014 — channel format separator


@dataclass
class ParsedPrice:
    sku: str        # stable key derived from name, used to look up in DB
    name: str       # human-readable name (cleaned)
    price: int      # price in rubles
    raw_line: str   # original text line (for debugging)


# Regional Indicator letters (flag emoji building blocks): U+1F1E0–U+1F1FF
_FLAG_RE = re.compile(r"[\U0001F1E0-\U0001F1FF]{2}\s*", re.UNICODE)

# "Рус 🔌" or "Рус🔌" suffix (Russian-locale hardware indicator)
_RUS_PLUG_RE = re.compile(r"\s*Рус\s*[\U0001F50C\U0001F9F9🔌]*", re.UNICODE)

# "(мятая 📦)" / "(после сервиса)" / "(скол)" / "(Без 📦)" / "(чуть порвана пломба)" etc.
SKIP_CONDITION_NOTES = True
_CONDITION_RE = re.compile(
    r"\s*\([^()]*(?:мятая|после\s+сервиса|скол|Без\s+📦|порвана)[^()]*\)",
    re.IGNORECASE | re.UNICODE,
)

# "(только Wi-Fi)" note — strip it, doesn't affect price identity
_WIFI_NOTE_RE = re.compile(r"\s*\(только[^)]*\)", re.UNICODE)

# "[MODEL_CODE]" at start of cleaned string
_MODEL_CODE_RE = re.compile(r"^\[[^\]]+\]\s*")

# Leading junk: backtick, dot, space — before the emoji/letter start
_LEAD_JUNK_RE = re.compile(r'^[`.\s]+')

# Any trailing non-letter, non-digit remnant (stray punctuation/emoji)
_TAIL_JUNK_RE = re.compile(r'[\s,;.!?🔌]+$', re.UNICODE)

# Bot format: "Name - 99.200₽" — price with dots as thousands separators + ₽ sign
# Greedy (.+) so we split at the LAST " - " before the price
_BOT_LINE_RE = re.compile(r"^(.+)\s+-\s+(\d[\d.]*)\s*₽", re.UNICODE)


def _strip_leading_nonword(s: str) -> str:
    """Advance past leading emoji and other non-letter/non-digit characters."""
    i = 0
    while i < len(s):
        c = s[i]
        cat = unicodedata.category(c)
        if cat.startswith("L") or cat == "Nd" or c == "[":
            break
        i += 1
    return s[i:]


def _clean_name(raw: str) -> str:
    """Strip emoji prefix, model code, flags, and locale notes from a raw name string."""
    s = _LEAD_JUNK_RE.sub("", raw)
    s = _strip_leading_nonword(s)
    s = _MODEL_CODE_RE.sub("", s)
    s = _FLAG_RE.sub("", s)
    s = _RUS_PLUG_RE.sub("", s)
    s = _WIFI_NOTE_RE.sub("", s)
    s = _TAIL_JUNK_RE.sub("", s)
    return s.strip()


def make_sku(text: str) -> str:
    """Derive a stable lowercase SKU from a product name string."""
    text = text.lower().strip()
    text = re.sub(r"[^a-zа-яёё0-9]+", "_", text)
    return text.strip("_")


def _parse_channel_line(line: str) -> ParsedPrice | None:
    """Parse channel-format line: 'Name — 58000'."""
    if _EM_DASH not in line:
        return None

    parts = re.split(r"\s*" + _EM_DASH + r"\s*", line, maxsplit=1)
    if len(parts) != 2:
        return None
    name_raw, price_raw = parts

    if SKIP_CONDITION_NOTES and _CONDITION_RE.search(name_raw):
        logger.debug("Skipping condition-note line: %r", line)
        return None

    price_clean = price_raw.strip().lstrip("`")
    price_match = re.match(r"\d[\d\s\xa0]*", price_clean)
    if not price_match:
        return None
    price_str = re.sub(r"[\s\xa0]", "", price_match.group())
    try:
        price = int(price_str)
    except ValueError:
        return None

    if not (500 <= price <= 15_000_000):
        return None

    name = _clean_name(name_raw)
    if not name or len(name) < 3:
        return None

    return ParsedPrice(sku=make_sku(name), name=name, price=price, raw_line=line.strip("`"))


def _parse_bot_line(line: str) -> ParsedPrice | None:
    """Parse bot-format line: 'Name - 99.200₽'."""
    m = _BOT_LINE_RE.match(line)
    if not m:
        return None

    name_raw, price_dotted = m.group(1), m.group(2)

    if SKIP_CONDITION_NOTES and _CONDITION_RE.search(name_raw):
        logger.debug("Skipping condition-note line: %r", line)
        return None

    # Remove dots (thousands separators) → integer
    try:
        price = int(price_dotted.replace(".", ""))
    except ValueError:
        return None

    if not (500 <= price <= 15_000_000):
        return None

    name = _clean_name(name_raw)
    if not name or len(name) < 3:
        return None

    return ParsedPrice(sku=make_sku(name), name=name, price=price, raw_line=line)


def parse_prices_from_text(text: str) -> list[ParsedPrice]:
    """Extract all price entries from a block of text (channel or bot format)."""
    results: list[ParsedPrice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        entry = _parse_channel_line(line)
        if entry is None:
            entry = _parse_bot_line(line)
        if entry is not None:
            results.append(entry)

    logger.debug("parse_prices_from_text: found %d entries", len(results))
    return results
