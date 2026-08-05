"""
Tests for AvitoApiClient.
HTTP is mocked via FakeResponse — no real network calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.avito_api_client import AvitoApiClient, _MSG_LIMIT
from src.adapters.avito_auth import AvitoAuthClient


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeResponse:
    """Minimal async context-manager that mimics aiohttp.ClientResponse."""

    def __init__(self, status: int, data: dict | None = None, text: str = ""):
        self.status = status
        self._data = data
        self._text = text

    async def json(self, *, content_type=None):
        return self._data or {}

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _make_client(token: str = "test_token") -> tuple[AvitoApiClient, MagicMock]:
    """Return (client, mock_session) with a pre-seeded auth token."""
    auth = MagicMock(spec=AvitoAuthClient)
    auth.get_token = AsyncMock(return_value=token)
    auth.invalidate = AsyncMock(return_value="refreshed_token")

    client = AvitoApiClient(auth)
    mock_session = MagicMock()
    client._session = mock_session  # inject directly, skip start()
    return client, mock_session


def _set_response(mock_session: MagicMock, resp: FakeResponse) -> None:
    mock_session.request = MagicMock(return_value=resp)


# ── send_message ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_posts_correct_body():
    client, session = _make_client()
    _set_response(session, FakeResponse(200, {"id": "msg1", "type": "text"}))

    result = await client.send_message(user_id=99, chat_id="chat_abc", text="Привет!")

    session.request.assert_called_once()
    call_kwargs = session.request.call_args
    assert call_kwargs[0][0] == "POST"
    assert "/messenger/v1/accounts/99/chats/chat_abc/messages" in call_kwargs[0][1]
    assert call_kwargs[1]["json"] == {"message": {"text": "Привет!"}, "type": "text"}
    assert result["id"] == "msg1"


@pytest.mark.asyncio
async def test_send_message_truncates_long_text():
    client, session = _make_client()
    _set_response(session, FakeResponse(200, {}))

    long_text = "А" * 1500
    await client.send_message(user_id=99, chat_id="chat_abc", text=long_text)

    sent = session.request.call_args[1]["json"]["message"]["text"]
    assert len(sent) == _MSG_LIMIT


# ── get_chat / extract_sender_name ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_chat_returns_data():
    client, session = _make_client()
    chat_data = {"id": "chat_abc", "users": [{"id": 42, "name": "Иван"}]}
    _set_response(session, FakeResponse(200, chat_data))

    result = await client.get_chat(user_id=99, chat_id="chat_abc")
    assert result["id"] == "chat_abc"


def test_extract_sender_name_found():
    client, _ = _make_client()
    chat = {"users": [{"id": 42, "name": "Иван"}, {"id": 99, "name": "Продавец"}]}
    assert client.extract_sender_name(chat, sender_id=42) == "Иван"


def test_extract_sender_name_not_found_returns_id():
    client, _ = _make_client()
    chat = {"users": [{"id": 99, "name": "Продавец"}]}
    assert client.extract_sender_name(chat, sender_id=777) == "777"


def test_extract_sender_name_no_name_field():
    client, _ = _make_client()
    chat = {"users": [{"id": 42}]}  # name absent
    assert client.extract_sender_name(chat, sender_id=42) == "42"


# ── mark_read ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mark_read_calls_correct_endpoint():
    client, session = _make_client()
    _set_response(session, FakeResponse(200, {"ok": True}))

    await client.mark_read(user_id=99, chat_id="chat_abc")

    call = session.request.call_args
    assert call[0][0] == "POST"
    assert "/read" in call[0][1]


# ── get_self ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_self_returns_profile():
    client, session = _make_client()
    _set_response(session, FakeResponse(200, {"id": 99, "name": "Gulai Store"}))

    result = await client.get_self()
    assert result["id"] == 99


# ── 401 retry ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_401_refreshes_token_and_retries():
    auth = MagicMock(spec=AvitoAuthClient)
    auth.get_token = AsyncMock(side_effect=["expired_token", "refreshed_token"])
    auth.invalidate = AsyncMock(return_value="refreshed_token")

    client = AvitoApiClient(auth)
    mock_session = MagicMock()
    client._session = mock_session

    resp_401 = FakeResponse(401, text="Unauthorized")
    resp_ok  = FakeResponse(200, {"id": "msg2"})
    mock_session.request = MagicMock(side_effect=[resp_401, resp_ok])

    result = await client.send_message(99, "chat_x", "Hi")

    assert auth.invalidate.call_count == 1
    assert mock_session.request.call_count == 2
    assert result["id"] == "msg2"


@pytest.mark.asyncio
async def test_401_does_not_retry_twice():
    """Second 401 (after refresh) must raise, not loop forever."""
    auth = MagicMock(spec=AvitoAuthClient)
    auth.get_token = AsyncMock(return_value="token")
    auth.invalidate = AsyncMock(return_value="new_token")

    client = AvitoApiClient(auth)
    mock_session = MagicMock()
    client._session = mock_session

    resp_401 = FakeResponse(401, text="Unauthorized")
    mock_session.request = MagicMock(return_value=resp_401)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await client.send_message(99, "chat_x", "Hi")

    assert mock_session.request.call_count == 2  # original + one retry


# ── error propagation ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_500_raises_runtime_error():
    client, session = _make_client()
    _set_response(session, FakeResponse(500, text="Internal Server Error"))

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await client.send_message(99, "chat_x", "Hi")


# ── guard: start() not called ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_raises_if_session_not_started():
    auth = MagicMock(spec=AvitoAuthClient)
    auth.get_token = AsyncMock(return_value="tok")
    client = AvitoApiClient(auth)
    # _session is None — start() was never called

    with pytest.raises(RuntimeError, match="start\\(\\)"):
        await client.get_self()
