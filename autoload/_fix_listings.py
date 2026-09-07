"""
Patch listings_data.json extra_fields to match the format of the working original 30.
Targets new entries (store77-*, biggeek-*, large numeric IDs) only.
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).parent.parent
JSON_PATH = ROOT / "infoautodownload" / "listings_data.json"

# ── Helpers ────────────────────────────────────────────────────────────────

def _num(s: str) -> str:
    """Strip non-digit suffix: '16 ГБ' → '16', '512 ГБ' → '512'."""
    m = re.match(r"(\d+)", str(s))
    return m.group(1) if m else str(s)

def detect_chip(name: str) -> str:
    n = name.lower()
    for chip in ["m5 max", "m5 pro", "m5 ultra", "m5",
                 "m4 max", "m4 pro", "m4 ultra", "m4",
                 "m3 max", "m3 pro", "m3",
                 "a18 pro", "a18", "a17 pro", "a16", "a15 bionic"]:
        if chip in n:
            return "Apple " + chip.title()
    return ""

def detect_screen(name: str) -> str:
    # "13.6"" → "13.6", "15.3"" etc.
    m = re.search(r'(\d+[\.,]\d+)"', name)
    if m:
        return m.group(1).replace(",", ".")
    # Plain inch: "14"" → "14"
    m2 = re.search(r'(\d{2})"', name)
    if m2:
        return m2.group(1)
    # From name fragments: Air 13, Pro 14, Pro 16, iMac 24
    for size in ["13.6", "15.3", "14.2", "16.2"]:
        if size.replace(".", ",") in name or size in name:
            return size
    m3 = re.search(r'\b(13|14|15|16|24|27|11)\b', name)
    if m3:
        return m3.group(1)
    return ""

_SCREEN_RES = {
    "13.6": "2560x1664", "13": "2560x1664",
    "15.3": "2880x1864", "15": "2880x1864",
    "14.2": "3024x1964", "14": "3024x1964",
    "16.2": "3456x2234", "16": "3456x2234",
    "24":   "4480x2520", "27": "5120x2880",
}

_CHIP_CORES = {
    # (cpu_cores, gpu_tag)
    "apple m5 ultra": ("28", "Apple graphics 80-core"),
    "apple m5 max":   ("16", "Apple graphics 40-core"),
    "apple m5 pro":   ("14", "Apple graphics 20-core"),
    "apple m5":       ("10", "Apple graphics 10-core"),
    "apple m4 ultra": ("24", "Apple graphics 60-core"),
    "apple m4 max":   ("14", "Apple graphics 32-core"),
    "apple m4 pro":   ("12", "Apple graphics 20-core"),
    "apple m4":       ("10", "Apple graphics 10-core"),
    "apple m3 max":   ("16", "Apple graphics 40-core"),
    "apple m3 pro":   ("12", "Apple graphics 18-core"),
    "apple m3":       ("8",  "Apple graphics 10-core"),
    "apple a18 pro":  ("6",  "Apple graphics 6-core"),
    "apple a18":      ("6",  "Apple graphics 5-core"),
}

def chip_cores(chip_str: str):
    key = chip_str.lower()
    for k, v in _CHIP_CORES.items():
        if k in key:
            return v
    return ("", "")

def detect_ram(name: str) -> str:
    # "16 ГБ / 512 ГБ" — first number before ГБ that isn't storage-sized
    # Usually written as "16 ГБ" RAM then "/" then storage
    # Look for small numbers: ≤ 64 = RAM, large = storage
    matches = re.findall(r"(\d+)\s*(?:ГБ|GB)", name, re.IGNORECASE)
    for m in matches:
        if int(m) <= 64:
            return m
    return ""

def detect_storage_gb(name: str) -> str:
    # TB first
    m = re.search(r"(\d+)\s*(?:ТБ|TB)", name, re.IGNORECASE)
    if m:
        return str(int(m.group(1)) * 1000)
    # Large GB values
    matches = re.findall(r"(\d+)\s*(?:ГБ|GB)", name, re.IGNORECASE)
    for val in matches:
        if int(val) >= 128:
            return val
    return ""

def detect_model_name(name: str) -> str:
    n = name
    # MacBook
    m = re.search(r"MacBook\s+(Air|Pro|Neo)\s+(\d+)", n)
    if m:
        line, size = m.group(1), m.group(2)
        chip = detect_chip(n)
        chip_short = chip.replace("Apple ", "") if chip else ""
        year_m = re.search(r"20(\d\d)", n)
        year = "20" + year_m.group(1) if year_m else ""
        parts = [f"MacBook {line} {size}"]
        if year:
            parts.append(f"({year}")
            if chip_short:
                parts[-1] += f", {chip_short}"
            parts[-1] += ")"
        elif chip_short:
            parts.append(f"({chip_short})")
        return " ".join(parts)
    # iMac
    m2 = re.search(r"iMac\s+(\d+)", n)
    if m2:
        size = m2.group(1)
        chip = detect_chip(n)
        chip_short = chip.replace("Apple ", "").split()[0] if chip else ""
        year_m = re.search(r"20(\d\d)", n)
        year = "20" + year_m.group(1) if year_m else ""
        if year and chip_short:
            return f"iMac {size} ({year}, {chip_short})"
        return f"iMac {size}"
    # Mac Mini
    if "Mac Mini" in n or "Mac mini" in n:
        chip = detect_chip(n)
        chip_short = chip.replace("Apple ", "").split()[0] if chip else ""
        return f"Mac Mini ({chip_short})" if chip_short else "Mac Mini"
    # Mac Studio
    if "Mac Studio" in n:
        chip = detect_chip(n)
        chip_short = chip.replace("Apple ", "").split()[0] if chip else ""
        return f"Mac Studio ({chip_short})" if chip_short else "Mac Studio"
    # iPad Pro
    m3 = re.search(r"iPad\s+Pro\s+(\d+)", n)
    if m3:
        size = m3.group(1)
        year_m = re.search(r"20(\d\d)", n)
        year = "20" + year_m.group(1) if year_m else ""
        return f"iPad Pro {size} ({year})" if year else f"iPad Pro {size}"
    # iPad Air
    m4 = re.search(r"iPad\s+Air\s+(\d+)", n)
    if m4:
        size = m4.group(1)
        chip = detect_chip(n)
        chip_short = chip.replace("Apple ", "").split()[0] if chip else ""
        return f"iPad Air {size} ({chip_short})" if chip_short else f"iPad Air {size}"
    # iPad Mini
    if "iPad Mini" in n or "iPad mini" in n:
        m5 = re.search(r"Mini\s+(\d)", n)
        gen = m5.group(1) if m5 else ""
        return f"iPad mini {gen}" if gen else "iPad mini"
    # iPad (base)
    m6 = re.search(r"\biPad\s+(\d+)", n)
    if m6:
        gen = m6.group(1)
        year_m = re.search(r"20(\d\d)", n)
        year = "20" + year_m.group(1) if year_m else ""
        return f"iPad {gen} ({year})" if year else f"iPad {gen}"
    # iPhone
    m7 = re.search(r"iPhone\s+([\w\s]+?)(?:\s+\d+\s*(?:ГБ|GB)|$)", n)
    if m7:
        return "iPhone " + m7.group(1).strip()
    return ""

# ── Category-specific field builders ───────────────────────────────────────

def patch_macbook(name: str, extra: dict) -> None:
    chip = detect_chip(name)
    screen = detect_screen(name)
    ram = detect_ram(name) or _num(extra.get("RamSize", ""))
    storage = detect_storage_gb(name) or _num(extra.get("DriveSize", ""))
    cores, gpu = chip_cores(chip)
    model = detect_model_name(name)
    res = _SCREEN_RES.get(screen, "")

    if model:
        extra["Model"] = model
    if chip:
        extra["ProcessorLine"] = chip
        extra["Processor"] = chip
    if cores:
        extra["ProcessorCores"] = cores
    extra["VideocardType"] = "Встроенная"
    if gpu:
        extra["Videocard"] = gpu
    # Map rounded screen sizes to Apple's exact specs
    _APPLE_SCREEN_EXACT = {"13": "13.6", "15": "15.3", "14": "14.2", "16": "16.2"}
    screen = _APPLE_SCREEN_EXACT.get(screen, screen)
    res = _SCREEN_RES.get(screen, res)
    if screen:
        extra["ScreenSize"] = screen
    if res:
        extra["ScreenRes"] = res
    if storage:
        extra["DriveSize"] = storage
    if ram:
        extra["RamSize"] = ram
    extra["DrivesConfig"] = "SSD"
    extra["OperatingSystem"] = "macOS"
    extra["KBLayout"] = "Нет кириллицы"
    extra.setdefault("Set", "Зарядное устройство | Коробка")
    extra["ExtendedCondition"] = "Новое"
    extra["BoxSealed"] = "Да"

def patch_ipad(name: str, extra: dict) -> None:
    n = name.lower()
    model = detect_model_name(name)
    storage = detect_storage_gb(name) or _num(extra.get("MemorySize", ""))
    sim = "LTE" if "lte" in n else ("Wi-Fi" in name and "Wi-Fi" or "Нет")
    chip = detect_chip(name)

    # RAM by chip (typical iPad configs)
    ram_by_chip = {
        "apple m5 max": "16", "apple m5 pro": "16", "apple m5": "16",
        "apple m4 max": "16", "apple m4 pro": "16", "apple m4": "8",
        "apple m3": "8", "apple a18 pro": "8", "apple a18": "8",
        "apple a17 pro": "8", "apple a16": "8",
    }
    ram = ""
    chip_l = chip.lower()
    for k, v in ram_by_chip.items():
        if k in chip_l:
            ram = v
            break

    extra["GoodsType"] = "Планшеты"
    extra["ProductsType"] = "Планшет"
    extra["Brand"] = "Apple"
    if model:
        extra["Model"] = model
    if storage:
        extra["MemorySize"] = storage
    if ram:
        extra["RamSize"] = ram
    extra["SimSlot"] = sim if sim != "Wi-Fi" else "Нет"
    # If it says Wi-Fi explicitly — keep "Нет" (no SIM slot)
    if "wi-fi" in n and "lte" not in n:
        extra["SimSlot"] = "Нет"
    elif "lte" in n:
        extra["SimSlot"] = "LTE"
    extra["DeviceHistory"] = "Неактивированный"
    extra.setdefault("Set", "Коробка | Провод зарядки")
    extra["ExtendedCondition"] = "Новое"
    extra["BoxSealed"] = "Да"

def patch_imac(name: str, extra: dict) -> None:
    model = detect_model_name(name)
    screen = detect_screen(name)
    extra["GoodsSubType"] = "Моноблоки"
    extra.pop("GoodsType", None)
    extra.pop("ExtendedCondition", None)
    extra.pop("BoxSealed", None)
    extra["Brand"] = "Apple"
    if model:
        extra["Model"] = model
    if screen:
        extra["Diagonal"] = screen

def patch_mac_mini(name: str, extra: dict) -> None:
    model = detect_model_name(name)
    chip = detect_chip(name)
    ram = detect_ram(name)
    storage = detect_storage_gb(name)
    extra["GoodsSubType"] = "Системные блоки"
    extra.pop("GoodsType", None)
    extra["Brand"] = "Apple"
    if model:
        extra["Model"] = model
    if chip:
        extra["ProcessorLine"] = chip
        extra["Processor"] = chip
    if ram:
        extra["RamSize"] = ram
    if storage:
        extra["DriveSize"] = storage
    extra["ExtendedCondition"] = "Новое"
    extra["BoxSealed"] = "Да"

def patch_phone(name: str, extra: dict) -> None:
    n = name.lower()
    model = detect_model_name(name)
    storage = detect_storage_gb(name) or _num(extra.get("MemorySize", ""))
    nano = "nano" in n or "sim" in n

    extra["GoodsType"] = "Мобильные телефоны"
    extra["Vendor"] = extra.pop("Brand", extra.get("Vendor", "Apple"))
    if model:
        extra["Model"] = model
    if storage:
        extra["MemorySize"] = storage
    extra["SimConfig"] = "2 SIM (nano SIM + eSIM)" if nano else "eSIM"
    extra["DeviceHistory"] = "Неактивированный"
    extra.setdefault("Set", "Коробка | Провод зарядки")
    extra["ExtendedCondition"] = "Новое"
    extra["BoxSealed"] = "Да"

def patch_monitor(name: str, extra: dict, entry: dict) -> None:
    entry["category"] = "Мониторы и запчасти"
    extra["Brand"] = "Apple"
    extra["Model"] = "Studio Display"
    extra["ProductsType"] = "Мониторы"
    extra["Condition"] = "Новое"
    extra.pop("Diagonal", None)
    extra.pop("ExtendedCondition", None)
    extra.pop("BoxSealed", None)


def patch_dyson_vacuum(name: str, extra: dict) -> None:
    extra["ProductType"] = "Пылесосы и запчасти"
    extra["GoodsType"] = "Для дома"
    extra["GoodsSubType"] = "Пылесосы"
    # Wet vacuums get their own subtype
    n = name.lower()
    if "wash" in n or "моющий" in n:
        extra["ProductSubType"] = "Вертикальные"
    else:
        extra["ProductSubType"] = "Вертикальные"


def patch_dyson_hair(name: str, extra: dict) -> None:
    extra["GoodsType"] = "Средства для волос"


def patch_rayban(name: str, extra: dict) -> None:
    extra["Brand"] = "Ray-Ban"
    extra["GoodsType"] = "Очки"
    extra["GlassesStyle"] = "Wayfarer"
    extra["Gender"] = "Унисекс"


def patch_action_camera(name: str, extra: dict, entry: dict) -> None:
    n = name.lower()
    if "dji" in n:
        vendor = "DJI"
    elif "gopro" in n:
        vendor = "GoPro"
    elif "insta360" in n:
        vendor = "Insta360"
    else:
        vendor = ""
    if vendor:
        extra["Vendor"] = vendor

    # Extract model: everything after vendor name, before storage size
    model = ""
    m = re.search(rf"{vendor}\s+(.+?)(?:\s+\d+\s*ГБ|$)", name, re.IGNORECASE)
    if m:
        model = re.sub(
            r"\s+(Adventure Combo|Standard Combo|Standart Combo|Adventure|Standard)$",
            "", m.group(1).strip()
        ).strip()
    if model:
        extra["Model"] = model

    entry["category"] = "Аудио и видео"
    extra["GoodsType"] = "Видеокамеры"
    extra["ProductType"] = "Экшн-камеры"


def patch_lego(name: str, extra: dict) -> None:
    extra["Brand"] = "LEGO"
    extra["GoodsType"] = "Игрушки"
    extra["Toys"] = "Конструктор"
    n = name.lower()
    if "botanicals" in n:
        extra["SeriesLEGO"] = "Botanicals"
        extra["Thematics"] = "Природа"
    extra["Age"] = "18 лет и старше"

def patch_samsung_tablet(name: str, extra: dict, slug: str) -> None:
    slug_l = slug.lower()

    # Storage: prefer from title, fallback from slug
    storage = detect_storage_gb(name)
    if not storage:
        m = re.search(r"-(\d{3,4})gb", slug_l)
        if m and int(m.group(1)) >= 128:
            storage = m.group(1)
        else:
            m_tb = re.search(r"-(\d+)tb", slug_l)
            if m_tb:
                storage = str(int(m_tb.group(1)) * 1000)

    # RAM: slug format is "{screen}-{ram}-{storage}gb" e.g. "146-12-256gb" or "11-12-128gb"
    ram = ""
    m_ram = re.search(r"-(\d{2,3})-(\d{1,2})-\d{3,4}(?:gb|tb)", slug_l)
    if m_ram:
        ram = m_ram.group(2)

    # SimSlot: 5G slug → LTE, otherwise no SIM slot
    sim = "LTE" if "-5g-" in slug_l else "Нет"

    # Model from slug: tab-s11-ultra, tab-s11, etc.
    model = ""
    m_model = re.search(r"tab-(s\d+)(?:-(ultra|plus|fe))?", slug_l)
    if m_model:
        gen = m_model.group(1).upper()
        variant = m_model.group(2)
        model = f"Samsung Galaxy Tab {gen} {variant.capitalize()}" if variant else f"Samsung Galaxy Tab {gen}"

    extra["GoodsType"] = "Планшеты"
    extra["ProductsType"] = "Планшет"
    extra["Brand"] = "Samsung"
    if model:
        extra["Model"] = model
    if storage:
        extra["MemorySize"] = storage
    if ram:
        extra["RamSize"] = ram
    extra["SimSlot"] = sim
    extra["DeviceHistory"] = "Неактивированный"
    extra.setdefault("Set", "Коробка | Провод зарядки")
    extra["ExtendedCondition"] = "Новое"
    extra["BoxSealed"] = "Да"
    extra.pop("Condition", None)

# ── Main ───────────────────────────────────────────────────────────────────

with open(JSON_PATH, encoding="utf-8") as f:
    entries = json.load(f)

# Only patch NEW entries (not original short-ID ones)
ORIGINAL_IDS = {e["avito_id"] for e in entries
                if isinstance(e.get("avito_id"), int) and len(str(e["avito_id"])) <= 10}

patched = 0
for entry in entries:
    avito_id = entry.get("avito_id")
    internal_id = entry.get("internal_id", "")

    # Skip original working 30
    if avito_id in ORIGINAL_IDS:
        continue

    name = entry.get("title", "")
    cat = entry.get("category", "")
    extra = entry.get("extra_fields", {})
    n = name.lower()

    # Also fix old description issues in new entries
    desc = entry.get("description", "")
    new_desc = re.sub(r"❗\s*Перед выездом бронируйте устройство\.",
                      "⚠️ Перед выездом уточните наличие у менеджера.", desc, flags=re.IGNORECASE)
    new_desc = re.sub(r"п/эт\.,\s*магазин\s*№508", "2 этаж, магазин №200", new_desc)
    if new_desc != desc:
        entry["description"] = new_desc

    # Remove Avito-internal fields that shouldn't be in new listings
    for f in ["ListingFee", "ContactPhone", "ContactMethod", "MultiItem",
              "AvitoDateEnd", "AvitoStatus", "CompanyName", "EMail"]:
        extra.pop(f, None)

    if cat == "Ноутбуки" and "macbook" in n:
        patch_macbook(name, extra)
        patched += 1
    elif cat == "Настольные компьютеры" and "imac" in n:
        patch_imac(name, extra)
        patched += 1
    elif cat == "Настольные компьютеры" and ("mac mini" in n or "mac studio" in n):
        patch_mac_mini(name, extra)
        patched += 1
    elif cat in ("Мониторы", "Мониторы и запчасти"):
        patch_monitor(name, extra, entry)
        patched += 1
    elif cat == "Планшеты и электронные книги" and "apple" in n:
        patch_ipad(name, extra)
        patched += 1
    elif cat == "Планшеты и электронные книги" and "samsung" in n:
        patch_samsung_tablet(name, extra, internal_id)
        patched += 1
    elif cat in ("Телефоны", "Мобильные телефоны") and "pixel" in n:
        # Google Pixel: keep category "Телефоны" (Мобильные телефоны is not a valid leaf category)
        # fix MemorySize (slug format: {storage}gb-{ram}gb → first large value = storage)
        entry["category"] = "Телефоны"
        slug_l = (internal_id or "").lower()
        storage = ""
        m_store = re.search(r"-(\d{3,4})gb-\d{1,2}gb", slug_l)
        if m_store:
            storage = m_store.group(1)
        else:
            storage = detect_storage_gb(name) or _num(extra.get("MemorySize", ""))
        extra["GoodsType"] = "Мобильные телефоны"
        extra["Vendor"] = "Google"
        if storage:
            extra["MemorySize"] = storage
        extra["DeviceHistory"] = "Неактивированный"
        extra.setdefault("Set", "Коробка | Провод зарядки")
        extra["ExtendedCondition"] = "Новое"
        extra["BoxSealed"] = "Да"
        extra.pop("Condition", None)
        patched += 1
    elif cat == "Телефоны" and "samsung" in n and "galaxy" in n:
        # Samsung Galaxy smartphones: fix GoodsType (plural), fix MemorySize to storage
        # Slug format: ...-{ram}-gb-{storage}-gb-... or ...-{ram}-gb-{storage}-tb-...
        slug_l = (internal_id or "").lower()
        storage = ""
        m_gb = re.search(r"-(\d+)-gb-(\d+)-gb", slug_l)
        m_tb = re.search(r"-(\d+)-gb-(\d+)-tb", slug_l)
        if m_gb:
            storage = m_gb.group(2)
        elif m_tb:
            storage = str(int(m_tb.group(2)) * 1000)
        # Model from slug: galaxy-s26-ultra → "Samsung Galaxy S26 Ultra"
        model = ""
        m_model = re.search(r"galaxy-(s\d+)(?:-(ultra|plus|fe))?", slug_l)
        if m_model:
            gen = m_model.group(1).upper()
            variant = m_model.group(2)
            model = f"Samsung Galaxy {gen} {variant.capitalize()}" if variant else f"Samsung Galaxy {gen}"
        extra["GoodsType"] = "Мобильные телефоны"
        extra["Vendor"] = "Samsung"
        if model:
            extra["Model"] = model
        if storage:
            extra["MemorySize"] = storage
        extra["DeviceHistory"] = "Неактивированный"
        extra.setdefault("Set", "Коробка | Провод зарядки")
        extra["ExtendedCondition"] = "Новое"
        extra["BoxSealed"] = "Да"
        extra.pop("Condition", None)
        patched += 1
    elif cat == "Бытовая техника" and "dyson" in n:
        patch_dyson_vacuum(name, extra)
        patched += 1
    elif cat == "Красота и здоровье" and "dyson" in n:
        patch_dyson_hair(name, extra)
        patched += 1
    elif cat == "Одежда, обувь, аксессуары" and "ray-ban" in n:
        patch_rayban(name, extra)
        patched += 1
    elif cat in ("Фото- и видеотехника", "Экшн-камеры", "Аудио и видео") and any(v in n for v in ("dji", "gopro", "insta360")):
        patch_action_camera(name, extra, entry)
        patched += 1
    elif cat == "Хобби и отдых" and "lego" in n:
        patch_lego(name, extra)
        patched += 1
    elif cat == "Телефоны" and "iphone" in n:
        # For 12-digit avito_id phones (catalog imports in wrong Avito category):
        # strip phone-specific fields — they cause "Ошибка параметра" in wrong category
        if isinstance(avito_id, int) and len(str(avito_id)) >= 12:
            for f in ["GoodsType", "Vendor", "Model", "MemorySize",
                      "SimConfig", "DeviceHistory", "Set",
                      "BoxSealed", "ExtendedCondition"]:
                extra.pop(f, None)
        else:
            patch_phone(name, extra)
        patched += 1

with open(JSON_PATH, "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False, indent=2)

print(f"Patched {patched} new entries")
