"""
Avito startup tasks — run once when the bot process starts.

Two operations:
  1. verify_avito_credentials()  — calls /core/v1/accounts/self to confirm
                                   credentials are valid and log account info.
  2. setup_avito_webhook()       — registers the webhook URL with Avito if not
                                   already present. Safe to call on every restart
                                   (idempotent: checks existing subscriptions first).

Avito allows multiple webhook URLs per account. We only ensure OUR url is in the
list; we do not touch other URLs (may belong to other integrations or old entries).
"""

import logging

from .avito_api_client import AvitoApiClient

logger = logging.getLogger(__name__)


async def verify_avito_credentials(api: AvitoApiClient) -> int:
    """
    Verify that client_id / client_secret work and return the numeric seller user_id.

    Raises RuntimeError if the API call fails (bad credentials, network error, etc.).
    Call this before registering the webhook so a misconfigured .env fails early
    with a clear error rather than a mysterious 401 later.
    """
    try:
        profile = await api.get_self()
    except Exception as exc:
        raise RuntimeError(f"Avito credential check failed: {exc}") from exc

    user_id: int = profile.get("id")
    name: str = profile.get("name", "—")
    email: str = profile.get("email", "—")

    if not user_id:
        raise RuntimeError(f"Avito /accounts/self returned no 'id': {profile}")

    logger.info(
        "Avito credentials OK — account: %s (id=%s, email=%s)", name, user_id, email
    )
    return user_id


async def setup_avito_webhook(api: AvitoApiClient, webhook_url: str) -> None:
    """
    Ensure webhook_url is registered for messenger notifications.

    Flow:
      1. Fetch current subscriptions.
      2. If our URL is already there → log and return (idempotent).
      3. Otherwise → register, log.

    Avito webhook requirements (from docs):
      - URL must be HTTPS and publicly reachable.
      - Must respond 200 OK within 2 seconds to an empty POST body.
      - Register with POST /messenger/v3/webhook.
    """
    logger.info("Checking Avito webhook subscriptions...")
    try:
        subscriptions = await api.get_webhook_subscriptions()
    except Exception as exc:
        logger.warning("Could not fetch subscriptions (will try to register anyway): %s", exc)
        subscriptions = []

    registered_urls = {s.get("url", "") for s in subscriptions}
    logger.debug("Currently registered webhook URLs: %s", registered_urls)

    if webhook_url in registered_urls:
        logger.info("Avito webhook already registered: %s", webhook_url)
        return

    logger.info("Registering Avito webhook: %s", webhook_url)
    try:
        await api.register_webhook(webhook_url)
        logger.info("Avito webhook registered successfully.")
    except Exception as exc:
        # Non-fatal: the bot can still receive messages via polling as fallback,
        # but log it loudly so the operator knows to fix it.
        logger.error(
            "Failed to register Avito webhook %s: %s\n"
            "Bot will NOT receive real-time messages until this is fixed.",
            webhook_url, exc,
        )
