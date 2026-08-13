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
    markup_pct  REAL    NOT NULL DEFAULT 0,   -- % added on top (set per-SKU later)
    source      TEXT,                          -- 'group' | 'bot'
    raw_text    TEXT,
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


class PriceDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
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
    ) -> bool:
        """Insert or update a price entry. Returns True if the price changed."""
        async with self._db.execute("SELECT price FROM prices WHERE sku=?", (sku,)) as cur:
            row = await cur.fetchone()
        old_price: Optional[int] = row[0] if row else None
        changed = old_price != price

        await self._db.execute(
            """INSERT INTO prices (sku, name, price, source, raw_text, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(sku) DO UPDATE SET
                 name=excluded.name, price=excluded.price,
                 source=excluded.source, raw_text=excluded.raw_text,
                 updated_at=CURRENT_TIMESTAMP""",
            (sku, name, price, source, raw_text),
        )
        if changed:
            await self._db.execute(
                "INSERT INTO price_history (sku, old_price, new_price) VALUES (?,?,?)",
                (sku, old_price, price),
            )
        await self._db.commit()

        if changed:
            logger.info("Price updated: %s %s → %d ₽", sku, name, price)
        return changed

    async def prune_stale(
        self,
        run_started_at: str,
        min_seen: int = 50,
        sources_seen: set[str] | None = None,
    ) -> int:
        """Delete SKUs not updated in the current run (disappeared from all sources).

        Safety guards:
        - Skips deletion if fewer than min_seen rows were updated this run.
        - If sources_seen is provided, only prunes entries from those sources,
          preserving entries from sources that returned 0 results (e.g. bot offline).
        Returns number of deleted rows.
        """
        async with self._db.execute(
            "SELECT COUNT(*) FROM prices WHERE updated_at >= ?", (run_started_at,)
        ) as cur:
            (updated_count,) = await cur.fetchone()

        if updated_count < min_seen:
            logger.warning(
                "prune_stale skipped: only %d rows updated this run (min_seen=%d) — "
                "partial fetch, not deleting stale entries",
                updated_count, min_seen,
            )
            return 0

        if sources_seen is not None:
            # Only prune stale entries from sources that actually returned data.
            # Entries from sources with 0 results (e.g. bot offline) are preserved.
            placeholders = ",".join("?" * len(sources_seen))
            stale_sql = (
                f"SELECT COUNT(*) FROM prices "
                f"WHERE updated_at < ? AND source IN ({placeholders})"
            )
            delete_sql = (
                f"DELETE FROM prices "
                f"WHERE updated_at < ? AND source IN ({placeholders})"
            )
            params = (run_started_at, *sources_seen)
        else:
            stale_sql = "SELECT COUNT(*) FROM prices WHERE updated_at < ?"
            delete_sql = "DELETE FROM prices WHERE updated_at < ?"
            params = (run_started_at,)

        async with self._db.execute(stale_sql, params) as cur:
            (stale_count,) = await cur.fetchone()

        if stale_count:
            await self._db.execute(delete_sql, params)
            await self._db.commit()
            skipped_note = (
                f" (only from sources: {sorted(sources_seen)})" if sources_seen else ""
            )
            logger.info(
                "prune_stale: removed %d stale SKU(s)%s", stale_count, skipped_note
            )

        return stale_count

    async def set_markup(self, sku: str, markup_pct: float) -> None:
        """Set per-SKU markup percentage (applied on top of raw supplier price)."""
        await self._db.execute(
            "UPDATE prices SET markup_pct=? WHERE sku=?", (markup_pct, sku)
        )
        await self._db.commit()

    # ── Read (dialog engine) ──────────────────────────────────────────────────

    async def get_price(self, sku: str) -> Optional[int]:
        """Return the final price (raw + markup) for a SKU, or None if unknown."""
        async with self._db.execute(
            "SELECT price, markup_pct FROM prices WHERE sku=?", (sku,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        raw, markup = row[0], row[1]
        return round(raw * (1 + markup / 100))

    async def get_all(self) -> list[dict]:
        """Return all price entries with final prices applied."""
        async with self._db.execute(
            "SELECT sku, name, price, markup_pct, source, updated_at FROM prices ORDER BY sku"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["final_price"] = round(r["price"] * (1 + r["markup_pct"] / 100))
            result.append(d)
        return result
