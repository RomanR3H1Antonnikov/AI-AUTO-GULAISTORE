"""
Tests for AvitoAuthClient — token caching, refresh, invalidation.
All HTTP calls are mocked via unittest.mock.patch.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.avito_auth import AvitoAuthClient, _REFRESH_BUFFER


def _mock_response(token: str, expires_in: int = 86400):
    """Build a fake aiohttp response that returns a token JSON."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"access_token": token, "expires_in": expires_in})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(response):
    """Wrap response in a fake aiohttp.ClientSession context manager."""
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
async def test_first_call_fetches_token():
    client = AvitoAuthClient("cid", "csecret")
    resp = _mock_response("tok_abc")
    with patch("src.adapters.avito_auth.aiohttp.ClientSession", return_value=_mock_session(resp)):
        token = await client.get_token()
    assert token == "tok_abc"


@pytest.mark.asyncio
async def test_second_call_uses_cache():
    client = AvitoAuthClient("cid", "csecret")
    resp = _mock_response("tok_cached")
    with patch("src.adapters.avito_auth.aiohttp.ClientSession", return_value=_mock_session(resp)) as mock_cls:
        await client.get_token()
        await client.get_token()
        # Session should only have been created once
        assert mock_cls.call_count == 1


@pytest.mark.asyncio
async def test_invalidate_forces_refresh():
    client = AvitoAuthClient("cid", "csecret")

    resp1 = _mock_response("tok_first")
    resp2 = _mock_response("tok_second")

    sessions = [_mock_session(resp1), _mock_session(resp2)]
    with patch("src.adapters.avito_auth.aiohttp.ClientSession", side_effect=sessions):
        t1 = await client.get_token()
        t2 = await client.invalidate()

    assert t1 == "tok_first"
    assert t2 == "tok_second"


@pytest.mark.asyncio
async def test_expired_token_refreshes():
    client = AvitoAuthClient("cid", "csecret")

    resp1 = _mock_response("tok_old", expires_in=_REFRESH_BUFFER - 1)
    resp2 = _mock_response("tok_new")

    sessions = [_mock_session(resp1), _mock_session(resp2)]
    with patch("src.adapters.avito_auth.aiohttp.ClientSession", side_effect=sessions):
        await client.get_token()
        # Token is considered expired because expires_in < _REFRESH_BUFFER
        token = await client.get_token()

    assert token == "tok_new"


@pytest.mark.asyncio
async def test_error_response_raises():
    client = AvitoAuthClient("cid", "bad_secret")
    resp = MagicMock()
    resp.status = 401
    resp.text = AsyncMock(return_value='{"error":"invalid_client"}')
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    with patch("src.adapters.avito_auth.aiohttp.ClientSession", return_value=_mock_session(resp)):
        with pytest.raises(RuntimeError, match="401"):
            await client.get_token()
