"""Fix ad_ids/internal_ids that contain spaces (invalid for Avito <Id> field).
Replaces spaces with hyphens in both avito_listings.yaml and listings_data.json.
"""
import json, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
YAML_PATH = ROOT / "data" / "avito_listings.yaml"
JSON_PATH = ROOT / "infoautodownload" / "listings_data.json"


def sanitize_id(id_str: str) -> str:
    return id_str.replace(" ", "-")


# ── Fix YAML ───────────────────────────────────────────────────────────────
yaml_lines = YAML_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
changed_yaml = 0
for i, line in enumerate(yaml_lines):
    # Match unquoted: "- ad_id: store77-X Y Z\n" or "  ad_id: store77-X Y Z\n"
    m = re.match(r"([\-\s]*ad_id:\s*)(.+?)(\s*)$", line.rstrip("\n"))
    if m and " " in m.group(2):
        old_id = m.group(2).strip()
        # Strip surrounding quotes if any
        if old_id.startswith("'") and old_id.endswith("'"):
            old_id = old_id[1:-1]
        new_id = sanitize_id(old_id)
        yaml_lines[i] = m.group(1) + new_id + "\n"
        print(f"YAML: {old_id!r} → {new_id!r}")
        changed_yaml += 1

YAML_PATH.write_text("".join(yaml_lines), encoding="utf-8")
print(f"Fixed {changed_yaml} YAML ad_ids\n")

# ── Fix JSON ───────────────────────────────────────────────────────────────
with open(JSON_PATH, encoding="utf-8") as f:
    entries = json.load(f)

changed_json = 0
for entry in entries:
    iid = entry.get("internal_id", "")
    if isinstance(iid, str) and " " in iid:
        new_iid = sanitize_id(iid)
        print(f"JSON: {iid!r} → {new_iid!r}")
        entry["internal_id"] = new_iid
        changed_json += 1

with open(JSON_PATH, "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False, indent=2)

print(f"\nFixed {changed_json} JSON internal_ids")
