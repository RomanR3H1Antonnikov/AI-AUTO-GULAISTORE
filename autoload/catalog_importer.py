"""
Avito catalog importer.

Reads infoautodownload/catalog_avito_no_duplicates.xlsx and outputs:
  1. data/avito_listings.yaml   — extended with new Apple listings (dynamic pricing)
  2. infoautodownload/listings_data.json — extended with new listing metadata
  3. autoload/catalog_feed.xml  — complete Avito Autoload XML for all 248 items

Items with 12-digit numeric IDs are treated as existing Avito listings (price updates).
Items with store77-* IDs are treated as new listings to be created on Avito.

Run:
    python -m autoload.catalog_importer
"""

import json
import re
import sqlite3
import sys
from pathlib import Path
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

import openpyxl
import yaml

ROOT = Path(__file__).parent.parent
CATALOG_XLS = ROOT / "infoautodownload" / "catalog_avito_no_duplicates.xlsx"
LISTINGS_YAML = ROOT / "data" / "avito_listings.yaml"
LISTINGS_JSON = ROOT / "infoautodownload" / "listings_data.json"
PRICES_DB = ROOT / "prices.db"
OUT_XML = ROOT / "autoload" / "catalog_feed.xml"

STORE_ADDRESS = "Москва, улица Барклая, 8"

# ── Avito category mapping ─────────────────────────────────────────────────

def avito_category(cat: str, subcat: str) -> str:
    subcat_l = subcat.lower()
    if cat == "Ноутбуки и компьютеры":
        if any(x in subcat_l for x in ["macbook", "ноутбук"]):
            return "Ноутбуки"
        if any(x in subcat_l for x in ["imac", "mac mini", "mac studio", "моноблок"]):
            return "Настольные компьютеры"
        if "монитор" in subcat_l:
            return "Мониторы"
        return "Ноутбуки"
    if cat == "Смартфоны":
        return "Телефоны"
    if cat == "Планшеты":
        return "Планшеты и электронные книги"
    if cat == "Наушники и аудиотехника":
        return "Аудио и видео"
    if cat == "Умный дом":
        return "Бытовая техника"
    if cat == "Экшн-камеры":
        return "Фото- и видеотехника"
    if cat == "Ray-Ban":
        return "Одежда, обувь, аксессуары"
    if cat == "Товары для красоты и ухода":
        return "Красота и здоровье"
    if cat == "Хобби и развлечения":
        return "Хобби и отдых"
    return cat

# ── Price SKU mapping (Apple products only) ────────────────────────────────

_IPHONE_AIR_COLORS = ["black", "blue", "gold", "white"]
_IPHONE_17_COLORS  = ["black", "blue", "lavender", "sage", "white"]
_IPHONE_PRO_COLORS = ["blue", "orange", "silver"]
_IPHONE_17E_COLORS = ["black", "pink", "white"]

def _is_nano(name: str) -> bool:
    return bool(re.search(r"nanosim|nano.?sim", name, re.IGNORECASE))

def _storage_tag(name: str) -> str:
    """Extract storage string: '256', '512', '1tb', '2tb'."""
    m = re.search(r"(\d+)\s*(тб|tb|гб|gb)", name, re.IGNORECASE)
    if not m:
        return ""
    num, unit = m.group(1), m.group(2).lower()
    if unit in ("тб", "tb"):
        return f"{num}tb"
    return m.group(1)  # GB → just digits

def _ram_tag(name: str) -> str:
    """First number followed by GB that isn't storage-sized."""
    m = re.search(r"(\d+)\s*гб[^/]", name, re.IGNORECASE)
    return m.group(1) if m else ""

def get_price_skus(name: str, brand: str) -> list[str]:
    """Return prices.db SKU list for an Apple product, empty for others."""
    if brand != "Apple":
        return []
    n = name.lower()

    # iPhone Air (17 Air / Air iPhone)
    if "air" in n and "iphone" in n and "ipad" not in n:
        st = _storage_tag(n)
        if not st:
            return []
        if "tb" in st:
            tag = "1tb" if "1" in st else "2tb"
        else:
            tag = st
        return [f"air_{tag}_{c}" for c in _IPHONE_AIR_COLORS]

    # iPhone 17e
    if "17e" in n or "iphone 17 e " in n:
        st = _storage_tag(n)
        suffix = "_nano" if _is_nano(n) else ""
        return [f"17e_{st}_{c}{suffix}" for c in _IPHONE_17E_COLORS if st]

    # iPhone 17 Pro Max
    if "17 pro max" in n or "17promax" in n:
        st = _storage_tag(n)
        suffix = "_nano" if _is_nano(n) else ""
        if st == "2":
            tag = "2tb"
        elif st == "1":
            tag = "1tb"
        else:
            tag = st
        return [f"17_pro_max_{tag}_{c}{suffix}" for c in _IPHONE_PRO_COLORS if tag]

    # iPhone 17 Pro
    if "17 pro" in n and "max" not in n:
        st = _storage_tag(n)
        suffix = "_nano" if _is_nano(n) else ""
        if st == "1":
            tag = "1tb"
        else:
            tag = st
        return [f"17_pro_{tag}_{c}{suffix}" for c in _IPHONE_PRO_COLORS if tag]

    # iPhone 17 (base)
    if re.search(r"iphone\s+17\b", n) and "pro" not in n and "air" not in n and "17e" not in n:
        st = _storage_tag(n)
        suffix = "_nano" if _is_nano(n) else ""
        return [f"17_{st}_{c}{suffix}" for c in _IPHONE_17_COLORS if st]

    # iPhone 16
    if re.search(r"iphone\s+16\b", n):
        st = _storage_tag(n)
        suffix = "_nano" if _is_nano(n) else ""
        return [f"16_{st}_{c}{suffix}" for c in ["black", "blue", "pink", "white"] if st]

    # iPad Pro 11 M5
    if "ipad pro 11" in n and "m5" in n:
        st = _storage_tag(n)
        conn = "lte" if "lte" in n else "wi_fi"
        return [f"ipad_pro_11_m5_{st}_{conn}_black", f"ipad_pro_11_m5_{st}_{conn}_silver"]

    # iPad Pro 13 M5
    if "ipad pro 13" in n and "m5" in n:
        st = _storage_tag(n)
        conn = "lte" if "lte" in n else "wi_fi"
        return [f"ipad_pro_13_m5_{st}_{conn}_black", f"ipad_pro_13_m5_{st}_{conn}_silver"]

    # iPad Air 11 M4
    if "ipad air 11" in n and "m4" in n:
        st = _storage_tag(n)
        conn = "lte" if "lte" in n else "wi_fi"
        return [f"ipad_air_11_m4_2026_{st}_{conn}_grey",
                f"ipad_air_11_m4_2026_{st}_{conn}_blue",
                f"ipad_air_11_m4_2026_{st}_{conn}_purple"]

    # iPad Air 13 M4
    if "ipad air 13" in n and "m4" in n:
        st = _storage_tag(n)
        conn = "lte" if "lte" in n else "wi_fi"
        return [f"ipad_air_13_m4_2026_{st}_{conn}_grey",
                f"ipad_air_13_m4_2026_{st}_{conn}_blue"]

    # iPad 11 (2025)
    if re.search(r"ipad\s+11", n) and "pro" not in n and "air" not in n:
        st = _storage_tag(n)
        conn = "lte" if "lte" in n else "wi_fi"
        return [f"ipad_11_2025_{st}_{conn}_blue",
                f"ipad_11_2025_{st}_{conn}_silver",
                f"ipad_11_2025_{st}_{conn}_pink"]

    # iPad Mini 7
    if "ipad mini" in n:
        st = _storage_tag(n)
        conn = "lte" if "lte" in n else "wi_fi"
        return [f"ipad_mini_7_{st}_{conn}_grey",
                f"ipad_mini_7_{st}_{conn}_blue"]

    return []

# ── Descriptions ───────────────────────────────────────────────────────────

_COMMON_FOOTER = (
    "\n\n✅ Новый, не активирован, заводские пломбы на месте."
    "\n✅ Гарантия магазина 12 месяцев."
    "\n✅ Кассовый чек."
    "\n✅ Проверка перед оплатой — без спешки."
    "\n\n📍 ТЦ «Горбушка», ул. Барклая 8, 2 этаж, магазин №200."
    "\nМ. Багратионовская, 5 минут пешком."
    "\n\n⚠️ Перед выездом уточните наличие у менеджера."
)

def make_description(name: str, brand: str, cat: str, price: int) -> str:
    av_cat = avito_category(cat, name)
    n = name.lower()

    if "macbook" in n or "ноутбук apple" in n:
        chip = ""
        for chip_name in ["M5 Max", "M5 Pro", "M5", "M4 Max", "M4 Pro", "M4",
                          "M3 Max", "M3 Pro", "M3", "A18 Pro"]:
            if chip_name.lower() in n:
                chip = chip_name
                break
        return (
            f"{name} — компактный и мощный ноутбук Apple с чипом {chip}. "
            f"Для работы, учёбы, дизайна и видеомонтажа."
            + _COMMON_FOOTER
        )

    if av_cat == "Настольные компьютеры":
        return (
            f"{name} — компьютер Apple для дома и офиса. "
            f"Высокая производительность, тихая работа, компактный дизайн."
            + _COMMON_FOOTER
        )

    if av_cat == "Мониторы":
        return (
            f"{name} — профессиональный монитор Apple с 5K дисплеем. "
            f"Для работы с графикой, фото и видео."
            + _COMMON_FOOTER
        )

    if av_cat == "Телефоны":
        return (
            f"{name}.\n\n"
            f"⭐ Большой выбор цветов в наличии.\n"
            f"⭐ Оригинал, новый, не активирован."
            + _COMMON_FOOTER
        )

    if av_cat == "Планшеты и электронные книги":
        return (
            f"{name} — планшет Apple для работы, учёбы и развлечений. "
            f"Быстрый чип, яркий экран, поддержка Apple Pencil."
            + _COMMON_FOOTER
        )

    if av_cat == "Аудио и видео":
        return (
            f"{name}.\n\nОригинальный товар, новый в упаковке."
            f"\n\nГарантия 12 месяцев. Кассовый чек."
            f"\n\n📍 ТЦ «Горбушка», ул. Барклая 8, 2 этаж, магазин №200."
            f"\nМ. Багратионовская, 5 минут пешком."
        )

    # Generic for Samsung, Dyson, DJI, etc.
    return (
        f"{name}.\n\nОригинальный товар, новый в упаковке."
        f"\n\nГарантия 12 месяцев. Кассовый чек."
        f"\n\n📍 ТЦ «Горбушка», ул. Барклая 8, 2 этаж, магазин №200."
        f"\nМ. Багратионовская, 5 минут пешком."
        f"\n\n⚠️ Перед выездом уточните наличие у менеджера."
    )

# ── Extra fields per Avito category ───────────────────────────────────────

def make_extra_fields(name: str, brand: str, cat: str) -> dict:
    av_cat = avito_category(cat, name)
    base = {
        "AdType": "Товар приобретен на продажу",
        "Condition": "Новое",
    }
    n = name.lower()

    if av_cat == "Ноутбуки":
        base["Vendor"] = brand
        # Keyboard layout — MacBook always Latin only
        if brand == "Apple":
            base["KBLayout"] = "Английская"
        base["BoxSealed"] = "В заводской упаковке"
        # RAM
        m = re.search(r"(\d+)\s*гб", n)
        if m:
            base["RamSize"] = m.group(1)
        # SSD — last number before SSD/tb
        m2 = re.search(r"(\d+)\s*(тб|tb)", n, re.IGNORECASE)
        if m2:
            val = int(m2.group(1))
            base["DriveSize"] = str(val * 1000)  # TB → GB for Avito
        else:
            m3 = re.search(r"(\d{3,4})\s*(гб|gb)", n, re.IGNORECASE)
            if m3:
                base["DriveSize"] = m3.group(1)
        # Screen size
        m4 = re.search(r'(\d+)"', name)
        if m4:
            base["ScreenSize"] = m4.group(1)
        return base

    if av_cat == "Телефоны":
        base["GoodsType"] = "Мобильный телефон"
        base["Vendor"] = brand
        # Storage
        st = _storage_tag(n)
        if st and "tb" not in st:
            base["MemorySize"] = st
        # SIM config
        if _is_nano(n):
            base["SimConfig"] = "Две SIM | SIM + eSIM"
        else:
            base["SimConfig"] = "Без SIM | eSIM + eSIM"
        base["BoxSealed"] = "В заводской упаковке"
        return base

    if av_cat == "Планшеты и электронные книги":
        base["GoodsType"] = "Планшет"
        base["Brand"] = brand
        st = _storage_tag(n)
        if st and "tb" not in st:
            base["MemorySize"] = st
        base["SimSlot"] = "LTE" if "lte" in n else "Wi-Fi"
        base["BoxSealed"] = "В заводской упаковке"
        return base

    if av_cat == "Настольные компьютеры":
        base["GoodsType"] = "Моноблок" if "imac" in n else "Системный блок"
        base["Brand"] = brand
        return base

    if av_cat == "Аудио и видео":
        base["GoodsType"] = "Наушники"
        return base

    return base

# ── Prices.db lookup ───────────────────────────────────────────────────────

def lookup_price(skus: list[str], markup: int = 0) -> int | None:
    if not skus:
        return None
    try:
        conn = sqlite3.connect(str(PRICES_DB))
        prices: list[int] = []
        for sku in skus:
            row = conn.execute(
                "SELECT price FROM prices WHERE sku=? AND available=1", (sku,)
            ).fetchone()
            if row:
                prices.append(row[0] + markup)
        conn.close()
        return min(prices) if prices else None
    except Exception as e:
        print(f"  [warn] prices.db lookup failed: {e}", file=sys.stderr)
        return None

# ── XML builder ────────────────────────────────────────────────────────────

_SKIP_XML = {"Address", "AdType", "ListingFee", "ContactPhone", "ImageNames",
             "ContactMethod", "AvitoDateEnd", "EMail", "CompanyName",
             "AvitoStatus", "MultiItem"}

def build_xml_ad(parent: Element, item_id: str, avito_id: int | None,
                 title: str, description: str, price: int,
                 av_cat: str, image_url: str, extra: dict) -> None:
    ad = SubElement(parent, "Ad")
    SubElement(ad, "Id").text = str(item_id)
    if avito_id:
        SubElement(ad, "AvitoId").text = str(avito_id)
    SubElement(ad, "Address").text = STORE_ADDRESS
    SubElement(ad, "Category").text = av_cat
    ad_type = extra.get("AdType", "Товар приобретен на продажу")
    SubElement(ad, "AdType").text = ad_type
    SubElement(ad, "Title").text = title[:50]  # Avito title limit
    SubElement(ad, "Description").text = description
    SubElement(ad, "Price").text = str(price)

    for field, value in extra.items():
        if field in _SKIP_XML:
            continue
        SubElement(ad, field).text = str(value)

    if image_url and image_url not in ("", "nan"):
        imgs = SubElement(ad, "Images")
        for url in image_url.split(" | "):
            url = url.strip()
            if url:
                img = SubElement(imgs, "Image")
                img.set("url", url)

# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    # Load existing data
    with open(LISTINGS_YAML, encoding="utf-8") as f:
        existing_yaml = yaml.safe_load(f)
    existing_listings: list[dict] = existing_yaml.get("listings", [])
    existing_ad_ids = {int(e["ad_id"]) for e in existing_listings}

    with open(LISTINGS_JSON, encoding="utf-8") as f:
        existing_json: list[dict] = json.load(f)
    existing_json_ids = {d["avito_id"] for d in existing_json}

    # Read catalog
    wb = openpyxl.load_workbook(CATALOG_XLS)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    new_yaml_entries: list[dict] = []
    new_json_entries: list[dict] = []

    root = Element("Ads")
    root.set("formatVersion", "3")
    root.set("target", "Avito.ru")

    skipped = 0
    total = 0

    for row in rows:
        if not row[0]:
            continue
        item_id, cat, subcat, brand, name, price_raw, _photo_local, photo_url, _src = row
        item_id = str(item_id).strip()
        name = str(name).strip()
        cat = str(cat).strip() if cat else ""
        subcat = str(subcat).strip() if subcat else ""
        brand = str(brand).strip() if brand else ""
        photo_url = str(photo_url).strip() if photo_url else ""
        try:
            price_catalog = int(float(str(price_raw).replace(" ", "").replace(",", ".")))
        except (ValueError, TypeError):
            price_catalog = 0

        # Determine if this is an existing Avito listing (numeric 12-digit ID)
        avito_id: int | None = None
        is_existing = False
        if re.fullmatch(r"\d{10,14}", item_id):
            avito_id = int(item_id)
            is_existing = True

        av_cat = avito_category(cat, subcat or name)
        skus = get_price_skus(name, brand)
        db_price = lookup_price(skus) if skus else None
        price = db_price if db_price else price_catalog
        if price == 0:
            skipped += 1
            continue

        description = make_description(name, brand, cat, price)
        extra = make_extra_fields(name, brand, cat)
        title = name[:50]

        # Add to avito_listings.yaml only if Apple (has price_skus for dynamic pricing)
        if is_existing and skus and avito_id not in existing_ad_ids:
            new_yaml_entries.append({
                "ad_id": avito_id,
                "title": title,
                "markup": 0,
                "price_skus": skus,
            })
            existing_ad_ids.add(avito_id)

        # Add to listings_data.json if existing listing and not already there
        if is_existing and skus and avito_id not in existing_json_ids:
            new_json_entries.append({
                "avito_id": avito_id,
                "internal_id": item_id,
                "title": title,
                "price": price,
                "category": av_cat,
                "image_urls": photo_url,
                "description": description,
                "sheet": cat,
                "extra_fields": {**extra, "Address": STORE_ADDRESS},
            })
            existing_json_ids.add(avito_id)

        # Always add to XML feed
        build_xml_ad(root, item_id, avito_id, title, description,
                     price, av_cat, photo_url, extra)
        total += 1

    # Write XML
    xml_bytes = tostring(root, encoding="unicode")
    pretty = parseString(xml_bytes).toprettyxml(indent="    ", encoding=None)
    lines = pretty.split("\n")
    if lines[0].startswith("<?xml"):
        lines = lines[1:]
    xml_out = '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(lines)
    OUT_XML.write_text(xml_out, encoding="utf-8")
    print(f"✓ XML feed: {OUT_XML.name} — {total} ads ({skipped} skipped, no price)")

    # Update avito_listings.yaml
    if new_yaml_entries:
        existing_listings.extend(new_yaml_entries)
        with open(LISTINGS_YAML, "w", encoding="utf-8") as f:
            yaml.dump({"listings": existing_listings}, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        print(f"✓ avito_listings.yaml — added {len(new_yaml_entries)} new entries")
    else:
        print("  avito_listings.yaml — no new entries (all already present)")

    # Update listings_data.json
    if new_json_entries:
        existing_json.extend(new_json_entries)
        with open(LISTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump(existing_json, f, ensure_ascii=False, indent=2)
        print(f"✓ listings_data.json — added {len(new_json_entries)} new entries")
    else:
        print("  listings_data.json — no new entries (all already present)")

    # Summary
    print(f"\nNew YAML entries ({len(new_yaml_entries)}):")
    for e in new_yaml_entries:
        skus_str = ", ".join(e["price_skus"][:3]) + ("…" if len(e["price_skus"]) > 3 else "")
        print(f"  {e['ad_id']}  [{skus_str or 'static price'}]  {e['title'][:40]}")


if __name__ == "__main__":
    main()
