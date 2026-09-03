"""
One-time cleanup:
  Remove store77-* / biggeek-* entries that are duplicates of numeric-Avito-ID
  entries sharing the same price_skus (same product, two rows in the catalog).
  Keeps the numeric-ID version (updates existing listing) and drops the string-ID one.
"""
import json, yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent
YAML_PATH = ROOT / "data" / "avito_listings.yaml"
JSON_PATH = ROOT / "infoautodownload" / "listings_data.json"

with open(YAML_PATH, encoding="utf-8") as f:
    data = yaml.safe_load(f)
listings = data["listings"]

# Build index: frozenset(price_skus) → list of entries that share it
from collections import defaultdict
by_skus: dict = defaultdict(list)
for e in listings:
    skus = tuple(sorted(e.get("price_skus") or []))
    if skus:
        by_skus[skus].append(e)

# Find string-ID entries that duplicate a numeric-ID entry
ids_to_drop: set[str] = set()
for skus, group in by_skus.items():
    if len(group) < 2:
        continue
    has_numeric = any(str(e["ad_id"]).isdigit() for e in group)
    if has_numeric:
        for e in group:
            if not str(e["ad_id"]).isdigit():
                ids_to_drop.add(str(e["ad_id"]))
                print(f"  DROP duplicate {e['ad_id']}  (skus: {skus[0]}...)")

if not ids_to_drop:
    print("No duplicates found.")
else:
    print(f"\nRemoving {len(ids_to_drop)} duplicate entries from YAML and JSON...")

    before = len(listings)
    listings = [e for e in listings if str(e["ad_id"]) not in ids_to_drop]
    data["listings"] = listings
    with open(YAML_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"YAML: {before} → {len(listings)} entries")

    with open(JSON_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    before_j = len(entries)
    entries = [e for e in entries if str(e.get("internal_id", "")) not in ids_to_drop]
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"JSON: {before_j} → {len(entries)} entries")
