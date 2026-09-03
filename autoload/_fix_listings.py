"""
One-time cleanup script:
  1. Remove static-price (empty price_skus) entries added from catalog (large IDs)
  2. Update old descriptions in listings_data.json (address, no-бронь wording)

Run once: python autoload/_fix_listings.py
"""
import json, re, yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent
YAML_PATH  = ROOT / "data" / "avito_listings.yaml"
JSON_PATH  = ROOT / "infoautodownload" / "listings_data.json"

NEW_ADDR_TEXT = "📍 ТЦ «Горбушка», ул. Барклая 8, 2 этаж, магазин №200.\nМ. Багратионовская, 5 минут пешком."

# ── 1. YAML: remove large-ID (catalog) entries with empty price_skus ──────

with open(YAML_PATH, encoding="utf-8") as f:
    data = yaml.safe_load(f)

before = len(data["listings"])
data["listings"] = [
    e for e in data["listings"]
    if e.get("price_skus")                 # keep entries that have SKUs
    or int(e["ad_id"]) < 100_000_000_000  # always keep original short-ID entries
]
after = len(data["listings"])

with open(YAML_PATH, "w", encoding="utf-8") as f:
    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f"YAML: {before} → {after} entries (removed {before - after} static-price catalog items)")

# ── 2. JSON: fix old descriptions in original entries (short IDs only) ────

with open(JSON_PATH, encoding="utf-8") as f:
    entries = json.load(f)

OLD_PHRASES = [
    (r"❗\s*Перед выездом (?:обязательно\s*)?уточняйте наличие и бронируйте устройство\.",
     "⚠️ Перед выездом уточните наличие у менеджера."),
    (r"Перед выездом бронируйте устройство\.",
     "Перед выездом уточните наличие у менеджера."),
    (r"п/эт\.,\s*магазин\s*№508",
     "2 этаж, магазин №200"),
    (r"полуторный\s+этаж,?\s*магазин\s*№508",
     "2 этаж, магазин №200"),
    (r"ТЦ «Горбушка»,\s*ул\.\s*Барклая\s*8,\s*2\s*эт[^,]*\.?,?\s*магазин\s*№200(?!,?\s*М)",
     "ТЦ «Горбушка», ул. Барклая 8, 2 этаж, магазин №200"),
]

fixed_count = 0
for entry in entries:
    if int(entry["avito_id"]) > 100_000_000_000:
        continue  # new entries already have correct descriptions
    desc = entry.get("description", "")
    new_desc = desc
    for pattern, replacement in OLD_PHRASES:
        new_desc = re.sub(pattern, replacement, new_desc, flags=re.IGNORECASE)
    if new_desc != desc:
        entry["description"] = new_desc
        fixed_count += 1

# Also remove static-price entries from JSON (same logic as YAML)
before_json = len(entries)
# Find ad_ids that are in the cleaned YAML
valid_ids = {e["ad_id"] for e in data["listings"]}
entries_clean = [
    e for e in entries
    if int(e["avito_id"]) < 100_000_000_000   # original entries: always keep
    or int(e["avito_id"]) in valid_ids          # new entries: only if in YAML
]
after_json = len(entries_clean)

with open(JSON_PATH, "w", encoding="utf-8") as f:
    json.dump(entries_clean, f, ensure_ascii=False, indent=2)

print(f"JSON: {before_json} → {after_json} entries (removed {before_json - after_json} static-price items)")
print(f"JSON: fixed descriptions in {fixed_count} original entries")
