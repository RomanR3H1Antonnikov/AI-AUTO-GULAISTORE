"""
Tests for AvitoWebhookServer.

HTTP layer: tested via httpx.AsyncClient + ASGITransport (no real network).
Business logic (_parse, _process): tested by calling methods directly with
mocked engine/transport so we don't spin up a server at all.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.adapters.avito_webhook_server import AvitoWebhookServer
from src.core.transport import IncomingMessage


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_server(reply: str | None = "Ответ бота") -> tuple[AvitoWebhookServer, MagicMock, MagicMock]:
    engine = MagicMock()
    engine.process_message = AsyncMock(return_value=reply)
    # db helpers used by _maybe_inject_item_context
    engine.db.get_dialog = AsyncMock(return_value=None)
    engine.db.get_message_count = AsyncMock(return_value=0)

    transport = MagicMock()
    transport.send_message = AsyncMock()
    # is_bot_echo must return False so owner messages reach the engine in tests
    transport.is_bot_echo = MagicMock(return_value=False)
    # get_chat returns empty dict → no item context injected (title is empty)
    transport._api.get_chat = AsyncMock(return_value={})

    server = AvitoWebhookServer(engine=engine, transport=transport)
    return server, engine, transport


def _webhook_body(
    author_id: int = 42,
    user_id: int = 99,         # 99 = our seller
    chat_id: str = "chat_abc",
    text: str = "Привет",
    msg_type: str = "text",
    payload_type: str = "message",
) -> dict:
    return {
        "id": "evt_001",
        "timestamp": 1700000000,
        "version": "v1.1",
        "payload": {
            "type": payload_type,
            "value": {
                "author_id": author_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "chat_type": "u2i",
                "item_id": 123,
                "type": msg_type,
                "content": {"text": text},
                "created": 1700000000,
                "published_at": "2026-08-04T13:00:00Z",
            },
        },
    }


async def _post(server: AvitoWebhookServer, body: dict) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        resp = await client.post("/webhook/avito", json=body)
        return resp.status_code


# ── HTTP endpoint — always returns 200 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_returns_200_for_valid_message():
    server, _, _ = _make_server()
    status = await _post(server, _webhook_body())
    assert status == 200


@pytest.mark.asyncio
async def test_webhook_returns_200_for_unsupported_type():
    """Voice/image/system events must still get 200 (don't let Avito retry forever)."""
    server, _, _ = _make_server()
    status = await _post(server, _webhook_body(msg_type="voice"))
    assert status == 200


@pytest.mark.asyncio
async def test_webhook_returns_200_for_malformed_json():
    """Non-JSON body → 200, not 400/500 (so Avito stops retrying)."""
    server, _, _ = _make_server()
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhook/avito",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint():
    server, _, _ = _make_server()
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── _parse: correct IncomingMessage ───────────────────────────────────────────

def test_parse_buyer_message():
    server, _, _ = _make_server()
    msg = server._parse(_webhook_body(author_id=42, user_id=99, text="Есть MacBook?"))
    assert msg is not None
    assert msg.dialog_id == "chat_abc"
    assert msg.sender_id == "42"
    assert msg.text == "Есть MacBook?"
    assert msg.is_owner_message is False
    assert msg.transport_name == "avito"


def test_parse_owner_message_sets_flag():
    """When seller writes in their own chat → is_owner_message=True."""
    server, _, _ = _make_server()
    msg = server._parse(_webhook_body(author_id=99, user_id=99))
    assert msg is not None
    assert msg.is_owner_message is True


def test_parse_strips_whitespace():
    server, _, _ = _make_server()
    msg = server._parse(_webhook_body(text="  Привет  \n"))
    assert msg is not None
    assert msg.text == "Привет"


def test_parse_skips_non_message_payload():
    server, _, _ = _make_server()
    with pytest.raises(ValueError, match="payload.type"):
        server._parse(_webhook_body(payload_type="notification"))


def test_parse_skips_voice():
    server, _, _ = _make_server()
    with pytest.raises(ValueError, match="voice"):
        server._parse(_webhook_body(msg_type="voice"))


def test_parse_skips_system():
    server, _, _ = _make_server()
    with pytest.raises(ValueError, match="system"):
        server._parse(_webhook_body(msg_type="system"))


def test_parse_skips_deleted():
    server, _, _ = _make_server()
    with pytest.raises(ValueError, match="deleted"):
        server._parse(_webhook_body(msg_type="deleted"))


def test_parse_skips_empty_text():
    server, _, _ = _make_server()
    body = _webhook_body(text="   ")
    with pytest.raises(ValueError, match="Empty text"):
        server._parse(body)


def test_parse_skips_null_content():
    server, _, _ = _make_server()
    body = _webhook_body()
    body["payload"]["value"]["content"] = None
    with pytest.raises(ValueError, match="Empty text"):
        server._parse(body)


# ── _process: engine + transport integration ──────────────────────────────────

@pytest.mark.asyncio
async def test_process_calls_engine_and_sends_reply():
    server, engine, transport = _make_server(reply="Да, MacBook Air есть!")

    await server._process(_webhook_body(text="Есть MacBook Air?"))
    await asyncio.sleep(0)  # let background tasks settle

    engine.process_message.assert_called_once()
    incoming: IncomingMessage = engine.process_message.call_args[0][1]
    assert incoming.text == "Есть MacBook Air?"
    assert incoming.dialog_id == "chat_abc"

    transport.send_message.assert_called_once_with("chat_abc", "Да, MacBook Air есть!")


@pytest.mark.asyncio
async def test_process_no_send_when_engine_returns_none():
    server, engine, transport = _make_server(reply=None)

    await server._process(_webhook_body())
    await asyncio.sleep(0)

    engine.process_message.assert_called_once()
    transport.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_skips_voice_without_calling_engine():
    server, engine, transport = _make_server()

    await server._process(_webhook_body(msg_type="voice"))

    engine.process_message.assert_not_called()
    transport.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_owner_message_triggers_auto_takeover():
    """Seller writing in their own chat → is_owner_message=True → engine handles takeover."""
    server, engine, transport = _make_server(reply=None)

    await server._process(_webhook_body(author_id=99, user_id=99, text="Уже договорились"))

    engine.process_message.assert_called_once()
    incoming: IncomingMessage = engine.process_message.call_args[0][1]
    assert incoming.is_owner_message is True


@pytest.mark.asyncio
async def test_process_engine_exception_does_not_crash():
    """Unhandled engine exception must be swallowed (background task can't propagate)."""
    server, engine, transport = _make_server()
    engine.process_message = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    # Should not raise
    await server._process(_webhook_body())


# ── Item context injection ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_item_context_injected_for_first_message():
    """First buyer message about a listing → title+price prepended to text."""
    server, engine, transport = _make_server()
    transport._api.get_chat = AsyncMock(return_value={
        "chat": {
            "context": {
                "value": {
                    "title": "MacBook Pro 14 M3",
                    "price": {"value": 150000},
                }
            }
        }
    })

    await server._process(_webhook_body(text="Актуально?"))

    incoming: IncomingMessage = engine.process_message.call_args[0][1]
    assert "[Покупатель пишет по объявлению: «MacBook Pro 14 M3»" in incoming.text
    assert "150" in incoming.text
    assert "Актуально?" in incoming.text


@pytest.mark.asyncio
async def test_item_context_not_injected_when_no_item_id():
    """Buyer messages from general chat (item_id=0) → text unchanged."""
    server, engine, transport = _make_server()
    body = _webhook_body(text="Привет")
    body["payload"]["value"]["item_id"] = 0

    await server._process(body)

    incoming: IncomingMessage = engine.process_message.call_args[0][1]
    assert incoming.text == "Привет"
    transport._api.get_chat.assert_not_called()


@pytest.mark.asyncio
async def test_item_context_not_injected_for_existing_dialog():
    """Second+ message in an existing dialog → no context injection."""
    server, engine, transport = _make_server()
    engine.db.get_dialog = AsyncMock(return_value={"id": 7})
    engine.db.get_message_count = AsyncMock(return_value=3)

    await server._process(_webhook_body(text="А доставка?"))

    incoming: IncomingMessage = engine.process_message.call_args[0][1]
    assert incoming.text == "А доставка?"
    transport._api.get_chat.assert_not_called()


# ── End-to-end: HTTP → background task → send ─────────────────────────────────

@pytest.mark.asyncio
async def test_end_to_end_message_is_processed():
    """POST /webhook/avito → 200 immediately, background task sends reply."""
    server, engine, transport = _make_server(reply="MacBook Air 13 есть!")

    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        resp = await client.post("/webhook/avito", json=_webhook_body(text="MacBook Air?"))

    assert resp.status_code == 200
    # Give the background task a chance to run
    await asyncio.sleep(0.05)

    transport.send_message.assert_called_once_with("chat_abc", "MacBook Air 13 есть!")
