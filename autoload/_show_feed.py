"""Show which listings are included / skipped in the feed."""
import json, sqlite3, yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent

with open(ROOT / "data" / "avito_listings.yaml", encoding="utf-8") as f:
    mappings = yaml.safe_load(f)["listings"]

with open(ROOT / "infoautodownload" / "listings_data.json", encoding="utf-8") as f:
    jmap = {e["avito_id"]: e for e in json.load(f) if isinstance(e["avito_id"], int)}

conn = sqlite3.connect(str(ROOT / "prices.db"))

included, skipped = [], []
for m in mappings:
    ad_id = int(m["ad_id"])
    skus = m.get("price_skus") or []
    prices = []
    for sku in skus:
        row = conn.execute("SELECT price FROM prices WHERE sku=? AND available=1", (sku,)).fetchone()
        if row:
            prices.append(row[0])
    price = min(prices) if prices else None
    title = jmap.get(ad_id, {}).get("title", m.get("title", "?"))[:45]
    if price:
        included.append((ad_id, price, title))
    else:
        reason = "no-skus" if not skus else "not-in-db"
        skipped.append((ad_id, reason, title))

conn.close()

print(f"INCLUDED ({len(included)} listings):")
for ad_id, price, title in sorted(included, key=lambda x: x[2]):
    print(f"  {ad_id}  {price:>8,} ₽  {title}")

print(f"\nSKIPPED ({len(skipped)}):")
for ad_id, reason, title in sorted(skipped, key=lambda x: x[1]):
    print(f"  {ad_id}  [{reason}]  {title}")
