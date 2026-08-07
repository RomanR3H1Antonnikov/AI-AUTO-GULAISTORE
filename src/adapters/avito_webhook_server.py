"""
Avito Messenger webhook receiver — FastAPI server.

Avito sends a POST to our URL for every new message and requires a response
within 2 seconds. We return 200 OK immediately and dispatch processing to a
background asyncio task so the LLM call never blocks the response.

Webhook envelope (v3):
  {
    "id": "...",
    "timestamp": 123,
    "version": "v1.1",
    "payload": {
      "type": "message",
      "value": { <WebhookMessage> }
    }
  }

Direction detection (no 'direction' field in webhook):
  author_id == user_id  →  seller wrote this  →  is_owner_message=True  →  auto-takeover
  author_id != user_id  →  buyer wrote this   →  process normally
"""

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..core.dialog_engine import DialogEngine
from ..core.transport import IncomingMessage, Transport

logger = logging.getLogger(__name__)

# Only text messages can be answered; everything else is silently skipped.
_ANSWERABLE_TYPES = {"text"}


class AvitoWebhookServer:
    """
    Wraps a FastAPI app that receives Avito messenger webhooks and feeds
    them into DialogEngine.

    Lifecycle: the ASGI app (self.app) is passed to uvicorn; no extra start/stop needed.
    """

    def __init__(
        self,
        engine: DialogEngine,
        transport: Transport,
    ) -> None:
        self.engine = engine
        self.transport = transport
        self.app = FastAPI(title="Gulai Store — Avito Webhook", docs_url=None, redoc_url=None)
        self._register_routes()

    def _register_routes(self) -> None:
        app = self.app

        @app.post("/webhook/avito")
        async def receive(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except Exception:
                logger.warning("Avito webhook: received non-JSON body, ignoring")
                return JSONResponse({"ok": True})

            # Fire-and-forget: must return 200 before the 2-second Avito timeout.
            asyncio.create_task(self._process(body))
            return JSONResponse({"ok": True})

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

    async def _process(self, body: dict) -> None:
        """Parse webhook body, run engine, send reply — all in background."""
        try:
            incoming = self._parse(body)
        except ValueError as exc:
            logger.debug("Webhook skipped: %s", exc)
            return

        if incoming is None:
            return

        # When the bot sends via API, Avito echoes it back as a webhook with
        # author_id == user_id — identical to the seller typing manually.
        # Suppress these echoes using the timestamp recorded in send_message().
        if incoming.is_owner_message and hasattr(self.transport, "is_bot_echo"):
            if self.transport.is_bot_echo(incoming.dialog_id):  # type: ignore[union-attr]
                logger.debug("Suppressed bot-echo webhook for chat %s", incoming.dialog_id)
                return

        try:
            reply = await self.engine.process_message(self.transport, incoming)
            if reply:
                await self.transport.send_message(incoming.dialog_id, reply)
        except Exception:
            logger.exception(
                "Unhandled error processing Avito chat %s", incoming.dialog_id
            )

    def _parse(self, body: dict) -> Optional[IncomingMessage]:
        """
        Convert raw webhook payload to IncomingMessage.

        Returns None for events we intentionally ignore.
        Raises ValueError with a description for anything structurally unexpected
        or for message types we skip (voice, image, system, deleted, …).
        """
        payload = body.get("payload", {})
        if payload.get("type") != "message":
            raise ValueError(f"payload.type is '{payload.get('type')}', expected 'message'")

        msg: dict = payload.get("value", {})
        msg_type: str = msg.get("type", "")

        if msg_type not in _ANSWERABLE_TYPES:
            raise ValueError(f"Skipping message type '{msg_type}'")

        text: Optional[str] = (msg.get("content") or {}).get("text")
        if not text or not text.strip():
            raise ValueError("Empty text in message content")

        author_id: int = msg["author_id"]
        user_id: int = msg["user_id"]  # always the seller account that registered the webhook
        chat_id: str = msg["chat_id"]

        return IncomingMessage(
            dialog_id=chat_id,
            sender_id=str(author_id),
            text=text.strip(),
            is_owner_message=(author_id == user_id),
            transport_name="avito",
            raw=body,
        )
