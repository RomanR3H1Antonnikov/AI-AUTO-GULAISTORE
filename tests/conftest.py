import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from src.storage.database import Database
from src.core.lead_detector import LeadDetector
from src.core.toxicity_detector import ToxicityDetector
from src.core.stock_source import StubStockSource
from src.core.dialog_engine import DialogEngine
from src.core.transport import IncomingMessage, Transport


# ── Database fixture (in-memory SQLite) ──────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


# ── OpenAI mock helpers ───────────────────────────────────────────────────────

def _make_completion(content: str):
    """Build a minimal mock OpenAI ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.total_tokens = 150
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _lead_response(is_lead: bool, confidence: str = "high", reason: str = "test"):
    return _make_completion(
        json.dumps({"is_lead": is_lead, "confidence": confidence, "reason": reason})
    )


def _toxic_response(is_toxic: bool, reason: str = "test"):
    return _make_completion(
        json.dumps({"is_toxic": is_toxic, "reason": reason})
    )


def _bot_reply(text: str):
    return _make_completion(text)


@pytest.fixture
def openai_mock():
    client = AsyncMock()
    return client


# ── Fake transport ─────────────────────────────────────────────────────────────

class FakeTransport(Transport):
    """In-memory transport for tests."""

    def __init__(self, transport_name: str = "test"):
        self._name = transport_name
        self.sent: list[tuple[str, str]] = []        # (dialog_id, text)
        self.owner_notifications: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def send_message(self, dialog_id: str, text: str) -> None:
        self.sent.append((dialog_id, text))

    async def send_owner_notification(self, text: str) -> None:
        self.owner_notifications.append(text)

    def get_dialog_link(self, dialog_id: str) -> str:
        return f"fake://dialog/{dialog_id}"

    async def get_sender_name(self, sender_id: str) -> str:
        return f"user_{sender_id}"


@pytest.fixture
def transport():
    return FakeTransport()


# ── DialogEngine fixture ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def engine(db, openai_mock):
    eng = DialogEngine(
        db=db,
        openai_client=openai_mock,
        knowledge_base_path="data/knowledge_base.yaml",
        catalog_path="data/catalog.yaml",
        config={
            "llm_model": "gpt-4o",
            "classifier_model": "gpt-4o-mini",
            "max_tokens_per_dialog_day": 100_000,
            "max_tokens_global_day": 1_000_000,
            "lead_notification_cooldown_minutes": 30,
        },
        stock_source=StubStockSource(),
    )
    return eng


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_msg(text: str, dialog_id: str = "chat_1", sender_id: str = "user_1",
             is_owner: bool = False) -> IncomingMessage:
    return IncomingMessage(
        dialog_id=dialog_id,
        sender_id=sender_id,
        text=text,
        is_owner_message=is_owner,
        transport_name="test",
    )
