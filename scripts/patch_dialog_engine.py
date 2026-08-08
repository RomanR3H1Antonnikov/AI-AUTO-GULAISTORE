"""Patch _format_catalog in dialog_engine.py to support markup-based pricing."""
import sys

FILEPATH = "src/core/dialog_engine.py"

NEW_METHOD = '''\
    async def _format_catalog(self) -> str:
        cat_notes = self._cat.get("category_notes", {})
        lines: list[str] = []
        for category, items in self._cat.get("categories", {}).items():
            note = cat_notes.get(category, "")
            header = f"\\n{category}" + (f" [{note}]" if note else "") + ":"
            lines.append(header)
            for item in items:
                name = item["name"]
                if item.get("config"):
                    name = f"{name} {item['config']}"
                if item.get("color"):
                    name = f"{name} ({item['color']})"

                if "markup" in item:
                    # New-style: final price = db_price + fixed markup
                    markup: int = item["markup"]
                    db_price: Optional[int] = None
                    sku = item.get("db_sku")
                    if self.price_db and sku:
                        try:
                            db_price = await self.price_db.get_price(sku)
                        except Exception:
                            logger.warning("price_db lookup failed for sku=%s", sku)
                    if db_price is not None:
                        price_str = f"{db_price + markup:,}".replace(",", " ")
                        lines.append(f"  • {name} — {price_str} ₽")
                    else:
                        lines.append(f"  • {name} — цена уточняется")
                else:
                    # Legacy-style: yaml price, optionally overridden by live price
                    yaml_price: int = item.get("price", 0)
                    live_price: Optional[int] = None
                    if self.price_db:
                        try:
                            live_price = await self.price_db.get_price(self._make_sku(item))
                        except Exception:
                            logger.warning("price_db lookup failed for %s", name)
                    final_price = live_price if live_price is not None else yaml_price
                    price_str = f"{final_price:,}".replace(",", " ")
                    lines.append(f"  • {name} — {price_str} ₽")
        return "\\n".join(lines)
'''

with open(FILEPATH, encoding="utf-8") as f:
    content = f.read()

# Find the method boundaries by line numbers
lines = content.splitlines(keepends=True)
start = None
end = None
for i, line in enumerate(lines):
    if "    async def _format_catalog(self) -> str:" in line:
        start = i
    if start is not None and i > start and line.strip() and not line.startswith("        ") and not line.startswith("    "):
        end = i
        break
    if start is not None and i > start and "async def _build_system_prompt" in line:
        end = i
        break

if start is None or end is None:
    print(f"ERROR: could not find method boundaries (start={start}, end={end})")
    sys.exit(1)

print(f"Replacing lines {start+1}..{end} (0-indexed {start}..{end-1})")
new_lines = lines[:start] + [NEW_METHOD + "\n"] + lines[end:]
new_content = "".join(new_lines)

with open(FILEPATH, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Done.")
