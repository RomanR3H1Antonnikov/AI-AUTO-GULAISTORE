"""
Price extraction from Telegram group messages.

Message format (from supplier price channel):
  💻[MODEL_CODE] Product Name (Config) Color🇺🇸🇭🇰 — PRICE
  iPad Product Name Config Color🇮🇳 — PRICE
  `🎧AirPods Product — PRICE`
  .iPad Product — PRICE

Lines not matching this pattern are silently skipped (headers, notes, etc.).
"""

import re
import unicodedata
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_EM_DASH = "—"  # —


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

# "(мятая 📦)" / "(после сервиса)" / "(скол на корпусе)" / "(Без 📦)" / etc.
# These indicate non-mint condition — we keep them in name so they get a distinct SKU.
# To SKIP these lines (not store damaged-packaging prices), set SKIP_CONDITION_NOTES=True.
SKIP_CONDITION_NOTES = True
_CONDITION_RE = re.compile(
    r"\s*\([^()]*(?:мятая|после\s+сервиса|скол|Без\s+📦|порвана)[^()]*\)",
    re.IGNORECASE | re.UNICODE,
)

# "(только Wi-Fi)" note — doesn't affect price identity for us, strip it
_WIFI_NOTE_RE = re.compile(r"\s*\(только[^)]*\)", re.UNICODE)

# "[MODEL_CODE]" at start of cleaned string
_MODEL_CODE_RE = re.compile(r"^\[[^\]]+\]\s*")

# Leading junk: backtick, dot, space — before the emoji/letter start
_LEAD_JUNK_RE = re.compile(r'^[`.\s]+')

# Any trailing non-letter, non-digit remnant (stray punctuation/emoji)
_TAIL_JUNK_RE = re.compile(r'[\s,;.!?🔌]+$', re.UNICODE)


def _strip_leading_nonword(s: str) -> str:
    """Advance past leading emoji and other non-letter/non-digit characters."""
    i = 0
    while i < len(s):
        c = s[i]
        cat = unicodedata.category(c)
        # Stop at: any letter (L*), decimal digit (Nd), or open bracket '['
        if cat.startswith("L") or cat == "Nd" or c == "[":
            break
        i += 1
    return s[i:]


def _clean_name(raw: str) -> str:
    """Strip emoji prefix, model code, flags, and locale notes from a raw name string."""
    s = _LEAD_JUNK_RE.sub("", raw)          # strip leading ` . space
    s = _strip_leading_nonword(s)            # skip leading emoji chars
    s = _MODEL_CODE_RE.sub("", s)           # remove [MODEL_CODE]
    s = _FLAG_RE.sub("", s)                 # remove flag emoji (country codes)
    s = _RUS_PLUG_RE.sub("", s)             # remove "Рус 🔌"
    s = _WIFI_NOTE_RE.sub("", s)            # remove "(только Wi-Fi)"
    s = _TAIL_JUNK_RE.sub("", s)            # trim trailing junk
    return s.strip()


def make_sku(text: str) -> str:
    """
    Derive a stable lowercase SKU from a product name string.
    Non-alphanumeric sequences become underscores.
    """
    text = text.lower().strip()
    text = re.sub(r"[^a-zа-яёё0-9]+", "_", text)
    return text.strip("_")


def parse_prices_from_text(text: str) -> list[ParsedPrice]:
    """Extract all price entries from a block of text."""
    results: list[ParsedPrice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _EM_DASH not in line:
            continue

        # Split on em-dash (optional surrounding spaces)
        parts = re.split(r"\s*" + _EM_DASH + r"\s*", line, maxsplit=1)
        if len(parts) != 2:
            continue
        name_raw, price_raw = parts

        # Skip condition-note lines (damaged packaging, post-service, etc.)
        if SKIP_CONDITION_NOTES and _CONDITION_RE.search(name_raw):
            logger.debug("Skipping condition-note line: %r", line)
            continue

        # Extract price: first run of digits (handles trailing backtick / emojis)
        price_clean = price_raw.strip().lstrip("`")
        price_match = re.match(r"\d[\d\s\xa0]*", price_clean)
        if not price_match:
            logger.debug("No numeric price in: %r", line)
            continue
        price_str = re.sub(r"[\s\xa0]", "", price_match.group())
        try:
            price = int(price_str)
        except ValueError:
            logger.debug("Non-integer price %r in: %r", price_str, line)
            continue
        if not (500 <= price <= 15_000_000):
            logger.debug("Price out of range (%d) in: %r", price, line)
            continue

        name = _clean_name(name_raw)
        if not name or len(name) < 3:
            logger.debug("Name too short after cleaning: %r → %r", name_raw, name)
            continue

        results.append(ParsedPrice(
            sku=make_sku(name),
            name=name,
            price=price,
            raw_line=line.strip("`"),
        ))

    logger.debug("parse_prices_from_text: found %d entries", len(results))
    return results
