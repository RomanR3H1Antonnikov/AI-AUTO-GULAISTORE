from abc import ABC, abstractmethod
from enum import Enum


class StockStatus(Enum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"
    OUT_OF_STOCK = "out_of_stock"


class StockSource(ABC):
    """
    Interface for checking product availability.
    Phase 1: StubStockSource (always returns UNKNOWN).
    Phase 2: GoogleSheetsStockSource — reads the supplier's live sheet.
    """

    @abstractmethod
    async def check(self, item_name: str) -> StockStatus:
        ...


class StubStockSource(StockSource):
    """
    Phase 1 stub. The bot will use the safe hedged response:
    'Да, эта модель у нас есть 😊 Актуальный остаток подтвержу перед вашим приездом'

    Replace with GoogleSheetsStockSource in Phase 2.
    Why hedged: a false "guaranteed in stock" causes wasted trips and bad reviews.
    The store's 4.9 rating is worth more than a confident answer.
    """

    async def check(self, item_name: str) -> StockStatus:
        return StockStatus.UNKNOWN
