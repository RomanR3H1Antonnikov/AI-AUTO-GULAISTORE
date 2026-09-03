import sqlite3
conn = sqlite3.connect("prices.db")

# All pro_14 SKUs
print("=== pro_14 ALL ===")
rows = conn.execute(
    "SELECT sku, price, available FROM prices WHERE sku LIKE 'pro_14%' ORDER BY sku"
).fetchall()
for r in rows:
    print(f"  {'✓' if r[2] else '✗'}  {r[0]}  {r[1]}")

# Check specific base M5 SKUs from catalog.yaml
print("\n=== base M5 16GB checks ===")
targets = [
    "pro_14_m5_16_512_black", "pro_14_m5_16_512_silver",
    "pro_14_m5_16_1tb_space_black", "pro_14_m5_10_10_16_1tb_silver",
    "pro_14_space_black_m5_32_1tb", "pro_14_silver_m5_32_1tb",
]
for sku in targets:
    row = conn.execute("SELECT price, available FROM prices WHERE sku=?", (sku,)).fetchone()
    if row:
        print(f"  {'✓' if row[1] else '✗'}  {sku}  {row[0]}")
    else:
        print(f"  —  {sku}  (not in db)")

conn.close()
