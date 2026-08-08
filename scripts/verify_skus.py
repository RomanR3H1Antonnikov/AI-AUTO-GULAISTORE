"""Verify which SKUs are present in prices.db — used to build catalog db_sku mappings."""
import asyncio
import sys

sys.path.insert(0, ".")
from src.storage.price_database import PriceDatabase

CHECK = [
    # iPhone 17 Pro Max
    "17_pro_max_256_blue", "17_pro_max_256_orange",
    "17_pro_max_512_blue", "17_pro_max_512_orange",
    "17_pro_max_1tb_blue", "17_pro_max_1tb_silver",
    "17_pro_max_2tb_silver", "17_pro_max_2tb_orange",
    # iPhone 17 Pro
    "17_pro_256_blue", "17_pro_256_silver", "17_pro_256_orange",
    "17_pro_512_blue", "17_pro_512_silver", "17_pro_512_orange",
    "17_pro_1tb_blue", "17_pro_1tb_silver", "17_pro_1tb_orange",
    # iPhone 17
    "17_256_black", "17_256_lavender",
    # iMac
    "imac_m4_10_10_24_1tb_silver", "imac_m4_10_10_32_512_silver",
    "imac_m4_10_10_32_1tb_silver", "imac_m4_10_10_32_2tb_silver",
    "imac_m4_10_10_16_256_silver", "imac_m4_10_10_24_256_green",
    "imac_m4_10_10_16_512_silver", "imac_m4_10_10_16_1tb_silver",
    "imac_m4_10_10_24_512_silver",
    # MacBook Air
    "air_15_m5_32_512_silver", "air_15_m5_32_2tb_silver", "air_15_m5_32_4tb_silver",
    "air_13_m5_32_2tb_70w_sky_blue", "air_13_m5_32_4tb_70w_silver",
    # MacBook Pro 16
    "pro_16_m5_max_18c_cpu_32c_gpu_36_2tb_black",
    "pro_16_m5_max_18c_cpu_40c_gpu_64_2tb_black",
    "pro_16_m5_max_18c_cpu_40c_gpu_64_4tb_silver",
    "pro_16_m5_max_18c_cpu_40c_gpu_128_8tb_black",
    # MacBook Pro 14
    "pro_14_m5_pro_18c_cpu_20c_gpu_48_1tb_black",
    "pro_14_m5_pro_18c_cpu_20c_gpu_48_2tb_black",
    "pro_14_m5_pro_18c_cpu_20c_gpu_64_1tb_black",
    "pro_14_m5_pro_18c_cpu_20c_gpu_64_2tb_black",
    "pro_14_m5_max_18c_cpu_40c_gpu_64_2tb_black",
    "pro_14_m5_max_18c_cpu_40c_gpu_64_4tb_black",
    "pro_14_m5_max_18c_cpu_40c_gpu_128_2tb_black",
    "pro_14_m5_max_18c_cpu_40c_gpu_128_4tb_black",
    "pro_14_m5_max_18c_cpu_40c_gpu_128_8tb_black",
    # iPad Pro
    "ipad_pro_11_m5_256_wi_fi_black", "ipad_pro_11_m5_256_lte_silver",
    "ipad_pro_11_m5_512_lte_black",
    "ipad_pro_13_m5_256_lte_black", "ipad_pro_13_m5_512_lte_black",
    "ipad_pro_13_m5_1tb_lte_black", "ipad_pro_13_m5_2tb_lte_black",
    "ipad_pro_13_m5_256_wi_fi_black", "ipad_pro_13_m5_512_wi_fi_black",
    "ipad_pro_13_m5_1tb_wi_fi_black", "ipad_pro_13_m5_2tb_wi_fi_black",
    # iPad Air 11 M4 2026
    "ipad_air_11_m4_2026_128_wi_fi_grey", "ipad_air_11_m4_2026_256_wi_fi_grey",
    "ipad_air_11_m4_2026_128_lte_grey", "ipad_air_11_m4_2026_256_lte_grey",
    # iPad Air 13 M4 2026
    "ipad_air_13_m4_2026_128_wi_fi_grey", "ipad_air_13_m4_2026_128_wi_fi_starlight",
    "ipad_air_13_m4_2026_128_lte_grey",
    "ipad_air_13_m4_2026_256_lte_grey", "ipad_air_13_m4_2026_512_lte_grey",
]


async def main() -> None:
    db = PriceDatabase("prices.db")
    await db.init()
    all_prices = await db.get_all()
    by_sku = {p["sku"]: p for p in all_prices}

    found, missing = 0, 0
    for sku in CHECK:
        entry = by_sku.get(sku)
        if entry:
            print(f"  OK  {entry['final_price']:>9}  {sku}")
            found += 1
        else:
            print(f"  --            {sku}")
            missing += 1

    print(f"\nFound: {found}, Missing: {missing}")
    await db.close()


asyncio.run(main())
