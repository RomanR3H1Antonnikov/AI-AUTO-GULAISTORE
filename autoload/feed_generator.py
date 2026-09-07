"""
Avito Autoload XML feed generator.

Reads listings_data.json (static listing metadata: images, descriptions,
category-specific fields), avito_listings.yaml (ad mapping), and
prices.db (live purchase prices), then emits a valid Avito Autoload XML v3 feed.

Pricing logic per listing entry:
  price_skus → min(db_price + markup) across available SKUs  (dynamic, Apple)
  static_price → fixed price from catalog                     (Samsung, Dyson, etc.)

Listings with neither price source, or where price_skus yields no DB hit and
no static_price fallback, are silently skipped.

ID logic:
  numeric ad_id  → <Id> + <AvitoId>  (updates existing Avito listing)
  string  ad_id  → <Id> only         (creates new Avito listing on first upload)
"""

import json
import logging
import sqlite3
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

import yaml

logger = logging.getLogger(__name__)

_SKIP_XML_FIELDS = {
    "Address", "AdType", "ListingFee", "ContactPhone", "ImageNames",
    "ContactMethod", "AvitoDateEnd", "EMail", "CompanyName",
    "AvitoStatus", "MultiItem",
}

_OPTION_FIELDS = {
    "Set", "Devicework", "Flaws", "DeviceFlaws",
    "CompFlaws", "FunctionsFlaws", "ConnFlaws",
}


def _load_listings_map(json_path: str) -> dict[str, dict]:
    """Key listings by internal_id (falls back to str(avito_id))."""
    with open(json_path, encoding="utf-8") as f:
        items = json.load(f)
    result: dict[str, dict] = {}
    for item in items:
        key = item.get("internal_id") or str(item.get("avito_id", ""))
        if key:
            result[key] = item
    return result


def _calc_price(price_skus: list[str], markup: int, db_path: str) -> int | None:
    """Return min(db_price + markup) across available SKUs, or None if nothing found."""
    if not price_skus:
        return None
    try:
        conn = sqlite3.connect(db_path)
        prices: list[int] = []
        for sku in price_skus:
            row = conn.execute(
                "SELECT price FROM prices WHERE sku=? AND available=1", (sku,)
            ).fetchone()
            if row:
                prices.append(row[0] + markup)
        conn.close()
        return min(prices) if prices else None
    except Exception:
        logger.exception("Failed to read prices.db at %s", db_path)
        return None


def _add_field(parent: Element, field: str, value: str) -> None:
    if field in _OPTION_FIELDS or " | " in value:
        el = SubElement(parent, field)
        for opt in value.split(" | "):
            opt = opt.strip()
            if opt:
                SubElement(el, "Option").text = opt
    else:
        SubElement(parent, field).text = value


def _build_ad(parent: Element, listing: dict, ad_id: str, price: int, listing_fee: str = "") -> None:
    ad = SubElement(parent, "Ad")

    SubElement(ad, "Id").text = ad_id

    # Only add AvitoId for numeric IDs (existing listings) — tells Avito to update, not create
    avito_id = listing.get("avito_id")
    if isinstance(avito_id, int):
        SubElement(ad, "AvitoId").text = str(avito_id)

    extra = listing.get("extra_fields", {})

    address = extra.get("Address", "Москва, улица Барклая, 8")
    SubElement(ad, "Address").text = address
    SubElement(ad, "Category").text = listing["category"]

    ad_type = extra.get("AdType", "Товар приобретен на продажу")
    SubElement(ad, "AdType").text = ad_type

    if listing_fee:
        SubElement(ad, "ListingFee").text = listing_fee

    SubElement(ad, "Title").text = listing["title"]
    SubElement(ad, "Description").text = listing["description"]
    SubElement(ad, "Price").text = str(price)

    for field, value in extra.items():
        if field in _SKIP_XML_FIELDS:
            continue
        _add_field(ad, field, str(value))

    image_urls_raw = listing.get("image_urls", "")
    if image_urls_raw and image_urls_raw not in ("nan", ""):
        urls = [u.strip() for u in image_urls_raw.split(" | ") if u.strip()]
        if urls:
            images_el = SubElement(ad, "Images")
            for url in urls:
                img = SubElement(images_el, "Image")
                img.set("url", url)


def generate_feed(
    listings_json: str,
    avito_yaml: str,
    prices_db: str,
) -> str:
    """Return complete Avito Autoload XML as a UTF-8 string."""
    listings_map = _load_listings_map(listings_json)

    with open(avito_yaml, encoding="utf-8") as f:
        mappings: list[dict] = yaml.safe_load(f)["listings"]

    root = Element("Ads")
    root.set("formatVersion", "3")
    root.set("target", "Avito.ru")

    included = 0
    skipped_no_price = 0
    skipped_no_listing = 0

    for mapping in mappings:
        ad_id = str(mapping["ad_id"])
        price_skus: list[str] = mapping.get("price_skus") or []
        markup = int(mapping.get("markup", 0))
        static_price: int = int(mapping.get("static_price", 0))

        # Resolve price: dynamic from DB first, fall back to static
        price = _calc_price(price_skus, markup, prices_db)
        if price is None and static_price > 0:
            price = static_price
        if price is None:
            logger.debug("Skip ad_id=%s — no price", ad_id)
            skipped_no_price += 1
            continue

        listing = listings_map.get(ad_id)
        if not listing:
            logger.warning("Skip ad_id=%s — not found in listings_data.json", ad_id)
            skipped_no_listing += 1
            continue

        listing_fee = str(mapping.get("listing_fee", ""))
        _build_ad(root, listing, ad_id, price, listing_fee)
        included += 1

    logger.info(
        "Feed: %d included, %d skipped (no price), %d skipped (no listing data)",
        included, skipped_no_price, skipped_no_listing,
    )

    xml_bytes = tostring(root, encoding="unicode")
    pretty = parseString(xml_bytes).toprettyxml(indent="    ", encoding=None)
    lines = pretty.split("\n")
    if lines[0].startswith("<?xml"):
        lines = lines[1:]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(lines)
