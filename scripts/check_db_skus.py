"""Verify that catalog-mapped SKUs exist in prices.db."""
import asyncio
import sys

sys.path.insert(0, ".")
from src.storage.price_database import PriceDatabase

CATALOG_SKUS = [
    "neo_8_256_blush",
    "neo_8_256_citrus",
    "neo_8_256_indigo",
    "air_13_m5_16_512_starlight",
    "air_13_m5_16_1tb_starlight",
    "air_15_m5_16_512_starlight",
    "air_15_m5_16_1tb_sky_blue",
    "air_15_m5_32_1tb_starlight",
    "pro_16_m5_pro_18c_cpu_20c_gpu_64_1tb_silver",
    "pro_16_m5_pro_18c_cpu_20c_gpu_64_2tb_silver",
    "imac_m4_8_8_16_256_silver",
    "imac_m4_8_8_16_512_silver",
]


async def main() -> None:
    db = PriceDatabase("prices.db")
    await db.init()
    all_prices = await db.get_all()
    by_sku = {p["sku"]: p for p in all_prices}

    print(f"Total entries in DB: {len(all_prices)}\n")
    print("Catalog SKU mapping check:")
    for sku in CATALOG_SKUS:
        entry = by_sku.get(sku)
        if entry:
            print(f"  OK  {sku}  =>  {entry['final_price']} rub  (src: {entry['source']})")
        else:
            print(f"  --  {sku}  (NOT IN DB)")

    await db.close()


asyncio.run(main())
