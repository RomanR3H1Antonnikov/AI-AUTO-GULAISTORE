"""Quick smoke test: print catalog as the bot would format it."""
import asyncio, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.insert(0, ".")
from openai import AsyncOpenAI
from src.storage.database import Database
from src.storage.price_database import PriceDatabase
from src.core.dialog_engine import DialogEngine
from src.core.stock_source import StubStockSource


async def main() -> None:
    db = Database(":memory:")
    await db.init()

    price_db = PriceDatabase("prices.db")
    await price_db.init()

    engine = DialogEngine(
        db=db,
        openai_client=AsyncOpenAI(api_key="test"),
        knowledge_base_path="data/knowledge_base.yaml",
        catalog_path="data/catalog.yaml",
        config={},
        stock_source=StubStockSource(),
        price_db=price_db,
    )

    catalog_text = await engine._format_catalog()
    lines = catalog_text.splitlines()
    unknown = [l for l in lines if "уточняется" in l]
    priced = [l for l in lines if "₽" in l]

    with open("scripts/catalog_preview.txt", "w", encoding="utf-8") as f:
        f.write(catalog_text)
        f.write(f"\n\n--- Total: {len(priced)} priced, {len(unknown)} pending ---\n")

    print(f"Written to scripts/catalog_preview.txt")
    print(f"Priced: {len(priced)}, Pending: {len(unknown)}")

    await db.close()
    await price_db.close()


asyncio.run(main())
