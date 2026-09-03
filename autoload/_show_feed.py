"""Show which listings are included / skipped in the feed, grouped by source."""
import json, sqlite3, yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent

with open(ROOT / "data" / "avito_listings.yaml", encoding="utf-8") as f:
    mappings = yaml.safe_load(f)["listings"]

with open(ROOT / "infoautodownload" / "listings_data.json", encoding="utf-8") as f:
    raw_json = json.load(f)

jmap = {}
for e in raw_json:
    key = e.get("internal_id") or str(e.get("avito_id", ""))
    if key:
        jmap[key] = e

conn = sqlite3.connect(str(ROOT / "prices.db"))

original, new_dynamic, new_static, skipped = [], [], [], []

for m in mappings:
    ad_id   = str(m["ad_id"])
    skus    = m.get("price_skus") or []
    markup  = int(m.get("markup", 0))
    s_price = int(m.get("static_price", 0))

    prices = []
    for sku in skus:
        row = conn.execute(
            "SELECT price FROM prices WHERE sku=? AND available=1", (sku,)
        ).fetchone()
        if row:
            prices.append(row[0] + markup)
    price = min(prices) if prices else (s_price or None)

    listing = jmap.get(ad_id)
    title = (listing or {}).get("title", m.get("title", "?"))[:45]

    # Original = short numeric ID (8-10 digits, pre-existing Avito listings)
    is_original = ad_id.isdigit() and len(ad_id) <= 10
    # New catalog = 12-14 digit numeric ID or store77-* string
    is_new_dynamic = not is_original and bool(skus)
    is_new_static  = not is_original and not skus

    if price is None:
        skipped.append((ad_id, "no-price", title))
    elif is_original:
        original.append((ad_id, price, title))
    elif is_new_dynamic:
        new_dynamic.append((ad_id, price, title))
    else:
        new_static.append((ad_id, price, title))

conn.close()

print(f"=== ORIGINAL (~30) — {len(original)} listings ===")
for ad_id, price, title in sorted(original, key=lambda x: x[2]):
    print(f"  {ad_id:<14} {price:>9,} ₽  {title}")

print(f"\n=== NEW DYNAMIC (Apple, prices.db) — {len(new_dynamic)} listings ===")
for ad_id, price, title in sorted(new_dynamic, key=lambda x: x[2]):
    print(f"  {ad_id:<14} {price:>9,} ₽  {title}")

print(f"\n=== NEW STATIC (catalog price) — {len(new_static)} listings ===")
for ad_id, price, title in sorted(new_static, key=lambda x: x[2]):
    print(f"  {ad_id:<14} {price:>9,} ₽  {title}")

print(f"\n=== SKIPPED — {len(skipped)} ===")
for ad_id, reason, title in skipped:
    print(f"  [{reason}]  {ad_id:<14}  {title}")

print(f"\nTOTAL: {len(original)+len(new_dynamic)+len(new_static)} included, {len(skipped)} skipped")
