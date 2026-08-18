"""
Shared price database — written by price_monitor, read by DialogEngine.

Both services access the same SQLite file (prices.db on VPS).
DialogEngine uses live prices to override YAML catalog prices.
PriceDatabase is optional: if the file doesn't exist, engine falls back to YAML.
"""

import logging
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS prices (
    sku         TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    price       INTEGER NOT NULL,
    markup_pct  REAL    NOT NULL DEFAULT 0,
    source      TEXT,
    raw_text    TEXT,
    available   INTEGER NOT NULL DEFAULT 1,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku         TEXT    NOT NULL,
    old_price   INTEGER,
    new_price   INTEGER NOT NULL,
    changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Migration for existing DBs that don't have the `available` column yet.
_MIGRATION_ADD_AVAILABLE = """
ALTER TABLE prices ADD COLUMN available INTEGER NOT NULL DEFAULT 1;
"""


class PriceDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

        # Migrate: add `available` column if missing (older DB versions).
        async with self._db.execute("PRAGMA table_info(prices)") as cur:
            cols = {row[1] async for row in cur}
        if "available" not in cols:
            await self._db.execute(_MIGRATION_ADD_AVAILABLE)
            await self._db.commit()
            logger.info("Migration: added `available` column to prices table")

        logger.info("PriceDatabase initialised at %s", self.path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Write (price monitor) ─────────────────────────────────────────────────

    async def upsert(
        self,
        sku: str,
        name: str,
        price: int,
        source: str,
        raw_text: str = "",
        available: int = 1,
    ) -> str:
        """Insert or update a price entry.

        Returns one of:
          "new"       — SKU seen for the first time
          "restored"  — SKU was marked unavailable (available=0), now back
          "changed"   — price changed
          "unchanged" — same price, already available
        """
        async with self._db.execute(
            "SELECT price, available FROM prices WHERE sku=?", (sku,)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            status = "new"
            old_price = None
        elif not row["available"]:
            status = "restored"
            old_price = row["price"]
        elif row["price"] != price:
            status = "changed"
            old_price = row["price"]
        else:
            status = "unchanged"
            old_price = row["price"]

        await self._db.execute(
            """INSERT INTO prices (sku, name, price, source, raw_text, available, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(sku) DO UPDATE SET
                 name=excluded.name, price=excluded.price,
                 source=excluded.source, raw_text=excluded.raw_text,
                 available=excluded.available,
                 updated_at=CURRENT_TIMESTAMP""",
            (sku, name, price, source, raw_text, available),
        )
        if status in ("new", "changed"):
            await self._db.execute(
                "INSERT INTO price_history (sku, old_price, new_price) VALUES (?,?,?)",
                (sku, old_price, price),
            )
        await self._db.commit()

        if status != "unchanged":
            logger.info("Price %s: %s %s → %d ₽", status, sku, name, price)
        return status

    async def prune_stale(
        self,
        run_started_at: str,
        min_seen: int = 50,
        sources_seen: set[str] | None = None,
    ) -> tuple[int, list[dict]]:
        """Soft-delete SKUs not updated in the current run (mark available=0).

        Previously this physically deleted rows; now rows are kept with available=0
        so the bot can distinguish "price unknown" from "price disappeared from feed".

        Safety guards:
        - Skips if fewer than min_seen rows were updated this run.
        - If sources_seen is provided, only marks entries from those sources,
          preserving entries from sources that returned 0 results.

        Returns (count_marked, list_of_disappeared) where each disappeared item is a
        dict with keys: sku, name, price (last known raw price).
        """
        async with self._db.execute(
            "SELECT COUNT(*) FROM prices WHERE updated_at >= ? AND available=1",
            (run_started_at,),
        ) as cur:
            (updated_count,) = await cur.fetchone()

        if updated_count < min_seen:
            logger.warning(
                "prune_stale skipped: only %d rows updated this run (min_seen=%d)",
                updated_count, min_seen,
            )
            return 0, []

        if sources_seen is not None:
            placeholders = ",".join("?" * len(sources_seen))
            stale_sql = (
                f"SELECT sku, name, price FROM prices "
                f"WHERE updated_at < ? AND available=1 AND source IN ({placeholders})"
            )
            mark_sql = (
                f"UPDATE prices SET available=0 "
                f"WHERE updated_at < ? AND available=1 AND source IN ({placeholders})"
            )
            params = (run_started_at, *sources_seen)
        else:
            stale_sql = (
                "SELECT sku, name, price FROM prices "
                "WHERE updated_at < ? AND available=1"
            )
            mark_sql = (
                "UPDATE prices SET available=0 "
                "WHERE updated_at < ? AND available=1"
            )
            params = (run_started_at,)

        async with self._db.execute(stale_sql, params) as cur:
            disappeared = [dict(r) async for r in cur]

        if disappeared:
            await self._db.execute(mark_sql, params)
            await self._db.commit()
            skipped_note = (
                f" (sources: {sorted(sources_seen)})" if sources_seen else ""
            )
            logger.info(
                "prune_stale: marked %d SKU(s) unavailable%s",
                len(disappeared), skipped_note,
            )

        return len(disappeared), disappeared

    async def set_markup(self, sku: str, markup_pct: float) -> None:
        """Set per-SKU markup percentage (applied on top of raw supplier price)."""
        await self._db.execute(
            "UPDATE prices SET markup_pct=? WHERE sku=?", (markup_pct, sku)
        )
        await self._db.commit()

    # ── Read (dialog engine) ──────────────────────────────────────────────────

    async def get_price(self, sku: str) -> Optional[int]:
        """Return the final price for an active SKU, or None if unknown/unavailable."""
        price, _ = await self.get_price_info(sku)
        return price

    async def get_price_info(self, sku: str) -> tuple[Optional[int], bool]:
        """Return (final_price_or_none, was_available_but_now_gone).

        Cases:
          (int,  False) — active price, use it
          (None, True)  — SKU known but currently unavailable (disappeared from feed)
          (None, False) — SKU never seen in the feed (catalog mapping error or new product)
        """
        async with self._db.execute(
            "SELECT price, markup_pct, available FROM prices WHERE sku=?", (sku,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None, False
        if not row["available"]:
            return None, True
        raw, markup = row["price"], row["markup_pct"]
        return round(raw * (1 + markup / 100)), False

    async def get_all(self) -> list[dict]:
        """Return all price entries (active only) with final prices applied."""
        async with self._db.execute(
            "SELECT sku, name, price, markup_pct, source, updated_at "
            "FROM prices WHERE available=1 ORDER BY sku"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["final_price"] = round(r["price"] * (1 + r["markup_pct"] / 100))
            result.append(d)
        return result
