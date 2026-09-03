"""
Fix listings_data.json:
  1. Old descriptions: replace бронируйте → уточните наличие, fix address
  2. MemorySize: strip trailing " ГБ" / " TB" to plain number (Avito wants numeric)
  3. BoxSealed: normalise to "Да" (what existing working listings use)
  4. Brand → Vendor for phones (Телефоны category)
  5. Add DeviceHistory=Неактивированный to Apple phone/tablet entries that lack it
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).parent.parent
JSON_PATH = ROOT / "infoautodownload" / "listings_data.json"

with open(JSON_PATH, encoding="utf-8") as f:
    entries = json.load(f)

desc_fixed = mem_fixed = box_fixed = vendor_fixed = history_fixed = 0

for entry in entries:
    extra = entry.get("extra_fields", {})
    cat = entry.get("category", "")

    # ── 1. Description fixes ────────────────────────────────────────────────
    desc = entry.get("description", "")
    new_desc = desc
    # HTML and plain-text variants of the booking line
    new_desc = re.sub(
        r"❗\s*Перед выездом (?:обязательно\s*)?уточняйте наличие и бронируйте устройство\.",
        "⚠️ Перед выездом уточните наличие у менеджера.",
        new_desc, flags=re.IGNORECASE,
    )
    new_desc = re.sub(
        r"❗\s*Перед выездом бронируйте устройство\.",
        "⚠️ Перед выездом уточните наличие у менеджера.",
        new_desc, flags=re.IGNORECASE,
    )
    new_desc = re.sub(
        r"Перед выездом бронируйте устройство\.",
        "Перед выездом уточните наличие у менеджера.",
        new_desc, flags=re.IGNORECASE,
    )
    # Old address variants
    new_desc = re.sub(r"п/эт\.,\s*магазин\s*№508", "2 этаж, магазин №200", new_desc)
    new_desc = re.sub(r"полуторный\s+этаж,?\s*магазин\s*№508", "2 этаж, магазин №200", new_desc)
    if new_desc != desc:
        entry["description"] = new_desc
        desc_fixed += 1

    # ── 2. MemorySize: strip unit suffix ───────────────────────────────────
    mem = str(extra.get("MemorySize", ""))
    if mem and not mem.isdigit():
        # "256 ГБ" → "256",  "1 ТБ" → "1000",  "2 ТБ" → "2000"
        m = re.match(r"(\d+)\s*(ГБ|GB|ТБ|TB)", mem, re.IGNORECASE)
        if m:
            num, unit = int(m.group(1)), m.group(2).upper()
            extra["MemorySize"] = str(num * 1000 if unit in ("ТБ", "TB") else num)
            mem_fixed += 1

    # ── 3. BoxSealed: normalise to "Да" ────────────────────────────────────
    if extra.get("BoxSealed") and extra["BoxSealed"] != "Да":
        extra["BoxSealed"] = "Да"
        box_fixed += 1

    # ── 4. Brand → Vendor for phones ───────────────────────────────────────
    if cat == "Телефоны" and "Brand" in extra and "Vendor" not in extra:
        extra["Vendor"] = extra.pop("Brand")
        vendor_fixed += 1

    # ── 5. DeviceHistory for Apple phones and tablets ──────────────────────
    is_apple = extra.get("Vendor") == "Apple" or extra.get("Brand") == "Apple"
    if is_apple and cat in ("Телефоны", "Планшеты и электронные книги"):
        if "DeviceHistory" not in extra:
            extra["DeviceHistory"] = "Неактивированный"
            history_fixed += 1

with open(JSON_PATH, "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False, indent=2)

print(f"Descriptions fixed:  {desc_fixed}")
print(f"MemorySize fixed:    {mem_fixed}")
print(f"BoxSealed fixed:     {box_fixed}")
print(f"Brand→Vendor fixed:  {vendor_fixed}")
print(f"DeviceHistory added: {history_fixed}")
print(f"Total entries:       {len(entries)}")
