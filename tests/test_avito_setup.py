"""
Tests for avito_setup: verify_avito_credentials and setup_avito_webhook.
All API calls are mocked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.avito_api_client import AvitoApiClient
from src.adapters.avito_setup import setup_avito_webhook, verify_avito_credentials


def _make_api(**overrides) -> MagicMock:
    api = MagicMock(spec=AvitoApiClient)
    api.get_self = AsyncMock(return_value={"id": 99, "name": "Gulai Store", "email": "test@avito.ru"})
    api.get_webhook_subscriptions = AsyncMock(return_value=[])
    api.register_webhook = AsyncMock()
    for attr, value in overrides.items():
        setattr(api, attr, value)
    return api


# ── verify_avito_credentials ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_returns_user_id():
    api = _make_api()
    user_id = await verify_avito_credentials(api)
    assert user_id == 99
    api.get_self.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_raises_on_api_error():
    api = _make_api()
    api.get_self = AsyncMock(side_effect=RuntimeError("401 Unauthorized"))

    with pytest.raises(RuntimeError, match="credential check failed"):
        await verify_avito_credentials(api)


@pytest.mark.asyncio
async def test_verify_raises_when_id_missing():
    api = _make_api()
    api.get_self = AsyncMock(return_value={"name": "No ID here"})  # 'id' absent

    with pytest.raises(RuntimeError, match="no 'id'"):
        await verify_avito_credentials(api)


# ── setup_avito_webhook: not yet registered ───────────────────────────────────

@pytest.mark.asyncio
async def test_registers_webhook_when_not_present():
    api = _make_api()
    api.get_webhook_subscriptions = AsyncMock(return_value=[])

    await setup_avito_webhook(api, "https://bot.example.ru/webhook/avito")

    api.register_webhook.assert_awaited_once_with("https://bot.example.ru/webhook/avito")


@pytest.mark.asyncio
async def test_registers_when_other_urls_exist():
    """Our URL is absent even though other URLs are registered."""
    api = _make_api()
    api.get_webhook_subscriptions = AsyncMock(return_value=[
        {"url": "https://other-service.ru/hook", "version": "3"},
    ])

    await setup_avito_webhook(api, "https://bot.example.ru/webhook/avito")

    api.register_webhook.assert_awaited_once_with("https://bot.example.ru/webhook/avito")


# ── setup_avito_webhook: already registered ───────────────────────────────────

@pytest.mark.asyncio
async def test_skips_registration_if_already_present():
    api = _make_api()
    api.get_webhook_subscriptions = AsyncMock(return_value=[
        {"url": "https://bot.example.ru/webhook/avito", "version": "3"},
    ])

    await setup_avito_webhook(api, "https://bot.example.ru/webhook/avito")

    api.register_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotent_with_multiple_existing_urls():
    """Our URL is already one of several registered — no re-registration."""
    api = _make_api()
    api.get_webhook_subscriptions = AsyncMock(return_value=[
        {"url": "https://old.example.ru/hook", "version": "3"},
        {"url": "https://bot.example.ru/webhook/avito", "version": "3"},
    ])

    await setup_avito_webhook(api, "https://bot.example.ru/webhook/avito")

    api.register_webhook.assert_not_awaited()


# ── error resilience ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscriptions_fetch_failure_still_tries_register():
    """If get_webhook_subscriptions fails, we attempt registration anyway (non-fatal)."""
    api = _make_api()
    api.get_webhook_subscriptions = AsyncMock(side_effect=RuntimeError("network timeout"))

    # Should not raise; should attempt registration
    await setup_avito_webhook(api, "https://bot.example.ru/webhook/avito")

    api.register_webhook.assert_awaited_once_with("https://bot.example.ru/webhook/avito")


@pytest.mark.asyncio
async def test_register_failure_does_not_raise():
    """Registration failure is logged but not fatal — bot can still work."""
    api = _make_api()
    api.get_webhook_subscriptions = AsyncMock(return_value=[])
    api.register_webhook = AsyncMock(side_effect=RuntimeError("Avito API unavailable"))

    # Must NOT raise
    await setup_avito_webhook(api, "https://bot.example.ru/webhook/avito")
