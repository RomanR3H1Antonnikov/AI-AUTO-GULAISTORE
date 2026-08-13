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
  MHFF4 MacBook Neo 13 2026 A18 Pro 8 256 Indigo - 56.800₽
  iPhone 17 Pro Max 256 Blue 1 Sim + eSim - 106.500₽🏎️

Both formats produce identical SKUs for the same product via _normalize_for_sku().
eSIM-only variants produce the base SKU; nano/physical-SIM variants get a _nano suffix.
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

# "(только Wi-Fi)" — пропускаем строку целиком: такой айпад не продаём,
# а его цена может случайно попасть в LTE-SKU если LTE есть в имени модели.
_WIFI_ONLY_RE = re.compile(r"\(только\s+wi[-\s]?fi\)", re.IGNORECASE | re.UNICODE)

# "(только ...)" прочие заметки — стрипаем (оставляем на случай других вариантов)
_WIFI_NOTE_RE = re.compile(r"\s*\(только[^)]*\)", re.UNICODE)

# "[MODEL_CODE]" at start of cleaned string (channel format: [MHFF4])
_MODEL_CODE_RE = re.compile(r"^\[[^\]]+\]\s*")

# Leading junk: backtick, dot, space — before the emoji/letter start
_LEAD_JUNK_RE = re.compile(r'^[`.\s]+')

# Any trailing non-letter, non-digit remnant (stray punctuation/emoji)
_TAIL_JUNK_RE = re.compile(r'[\s,;.!?🔌]+$', re.UNICODE)

# Bot format: "Name - 99.200₽" — price with dots as thousands separators + ₽ sign
# Greedy (.+) so we split at the LAST " - " before the price
_BOT_LINE_RE = re.compile(r"^(.+)\s+-\s+(\d[\d.]*)\s*₽", re.UNICODE)

# ── SKU normalisation (strips bot-specific noise so channel and bot match) ──

# Leading unbracketed model code: MHFF4, MDHA4, MVV83, MH304, etc.
# Pattern: 1-3 uppercase letters + 1-4 uppercase/digits + trailing digit
_LEAD_CODE_RE = re.compile(r"^[A-Z]{1,3}[A-Z0-9]{1,4}\d\s+", re.UNICODE)

# "MacBook " prefix (bot writes full name; channel writes "Neo ...", "Air ...")
_MACBOOK_PREFIX_RE = re.compile(r"^MacBook\s+", re.UNICODE)

# "iPhone " prefix (bot writes "iPhone 17 Pro Max"; channel writes "17 Pro Max")
_IPHONE_PREFIX_RE = re.compile(r"^iPhone\s+", re.UNICODE)

# "A18 Pro" / "A16" chip designation inside Neo names
_NEO_CHIP_RE = re.compile(r"\s+A\d+(?:\s+Pro)?\b", re.UNICODE)

# Screen-size digit right after "Neo" ("Neo 13 ...") — not part of the spec
_NEO_SCREEN_RE = re.compile(r"(?<=Neo)\s+1[35]\b", re.UNICODE)

# Year: 2024/2025/2026/2027
_YEAR_RE = re.compile(r"\s+\b20[2-3]\d\b", re.UNICODE)

# Trailing unbracketed model code at end of string: " MDWK4", " MH304"
_TRAIL_CODE_RE = re.compile(r"\s+[A-Z]{1,3}[A-Z0-9]{1,4}\d$", re.UNICODE)

# eSIM-only suffix → strip (eSIM is the base/default SKU)
_SIM_ESIM_RE = re.compile(r"\s+eSim\b.*$", re.IGNORECASE | re.UNICODE)

# Nano/physical SIM variants → replace suffix with " nano" to produce _nano SKU
_SIM_NANO_RE = re.compile(
    r"\s+(?:[12]\s*Sim(?:\s*\+\s*eSim)?|nanoSIM(?:\s*\+\s*eSIM)?).*$",
    re.IGNORECASE | re.UNICODE,
)


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
    s = _MODEL_CODE_RE.sub("", s)      # remove [MHFF4] (channel bracketed codes)
    s = _FLAG_RE.sub("", s)
    s = _RUS_PLUG_RE.sub("", s)
    s = _WIFI_NOTE_RE.sub("", s)
    s = _TAIL_JUNK_RE.sub("", s)
    return s.strip()


def _normalize_for_sku(name: str) -> str:
    """Convert a cleaned product name to canonical form before SKU generation.

    Both channel and bot produce the same SKU for the same product:
      channel "NEO (8/256) Indigo"                       → neo_8_256_indigo
      bot     "MHFF4 MacBook Neo 13 2026 A18 Pro 8 256 Indigo" → neo_8_256_indigo

      channel "17 Pro Max (256) Blue"                    → 17_pro_max_256_blue
      bot     "iPhone 17 Pro Max 256 Blue eSim"          → 17_pro_max_256_blue
      bot     "iPhone 17 Pro Max 256 Blue 1 Sim + eSim"  → 17_pro_max_256_blue_nano
    """
    s = _LEAD_CODE_RE.sub("", name)      # MHFF4, MDHA4, etc.
    s = _MACBOOK_PREFIX_RE.sub("", s)    # "MacBook "
    s = _IPHONE_PREFIX_RE.sub("", s)    # "iPhone "
    s = _NEO_CHIP_RE.sub("", s)         # "A18 Pro"
    s = _NEO_SCREEN_RE.sub("", s)       # "Neo 13" → "Neo"
    s = _YEAR_RE.sub("", s)             # 2026, 2025, …
    s = _TRAIL_CODE_RE.sub("", s)       # trailing "MDWK4"
    # SIM-type suffixes: nano variants become _nano SKU; eSIM-only strips cleanly
    if _SIM_NANO_RE.search(s):
        s = _SIM_NANO_RE.sub(" nano", s)
    else:
        s = _SIM_ESIM_RE.sub("", s)
    return s.strip()


def make_sku(text: str) -> str:
    """Derive a stable lowercase SKU from a product name string."""
    text = _normalize_for_sku(text)
    text = text.lower().strip()
    text = re.sub(r"[^a-zа-яёё0-9]+", "_", text)
    return text.strip("_")


def _parse_channel_line(line: str) -> "ParsedPrice | None":
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

    if _WIFI_ONLY_RE.search(name_raw):
        logger.debug("Skipping Wi-Fi-only line: %r", line)
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


def _parse_bot_line(line: str) -> "ParsedPrice | None":
    """Parse bot-format line: 'Name - 99.200₽'."""
    m = _BOT_LINE_RE.match(line)
    if not m:
        return None

    name_raw, price_dotted = m.group(1), m.group(2)

    if SKIP_CONDITION_NOTES and _CONDITION_RE.search(name_raw):
        logger.debug("Skipping condition-note line: %r", line)
        return None

    if _WIFI_ONLY_RE.search(name_raw):
        logger.debug("Skipping Wi-Fi-only line: %r", line)
        return None

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
