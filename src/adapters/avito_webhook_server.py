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
from collections import OrderedDict
from dataclasses import replace
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..core.dialog_engine import DialogEngine
from ..core.transport import IncomingMessage, Transport

logger = logging.getLogger(__name__)

# In-memory dedup for Avito duplicate webhook deliveries.
# Keyed by Avito message ID; FIFO eviction keeps memory bounded.
_SEEN_MSG_IDS: OrderedDict[str, bool] = OrderedDict()
_MAX_SEEN_IDS = 2000

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
        # Dedup: Avito occasionally delivers the same webhook twice.
        # Use the message's own Avito ID as the dedup key.
        avito_msg_id: str = str(
            (body.get("payload", {}).get("value") or {}).get("id", "")
        )
        if avito_msg_id:
            if avito_msg_id in _SEEN_MSG_IDS:
                logger.debug("Duplicate Avito message %s — skipped", avito_msg_id)
                return
            _SEEN_MSG_IDS[avito_msg_id] = True
            if len(_SEEN_MSG_IDS) > _MAX_SEEN_IDS:
                _SEEN_MSG_IDS.popitem(last=False)

        # System events (e.g. buyer viewed phone number) → proactive greeting
        if await self._maybe_greet_on_system_event(body):
            return

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

        if not incoming.is_owner_message:
            incoming = await self._maybe_inject_item_context(incoming, body)

        try:
            reply = await self.engine.process_message(self.transport, incoming)
            if reply:
                await self.transport.send_message(incoming.dialog_id, reply)
        except Exception:
            logger.exception(
                "Unhandled error processing Avito chat %s", incoming.dialog_id
            )

    async def _maybe_greet_on_system_event(self, body: dict) -> bool:
        """
        Handle Avito system messages (buyer viewed phone number, contact request, etc.).
        If the dialog has no prior messages, send a proactive greeting.
        Returns True if the body was a system event (so _process skips normal parsing).
        """
        msg = (body.get("payload", {}).get("value") or {})
        if msg.get("type") != "system":
            return False

        chat_id: str = msg.get("chat_id", "")
        if not chat_id:
            return False

        dialog = await self.engine.db.get_dialog("avito", chat_id)
        if dialog and await self.engine.db.get_message_count(dialog["id"]) > 0:
            logger.debug("System event in existing dialog %s — skipping greeting", chat_id)
            return True

        greeting = "Добрый день! Чем могу помочь? 😊"
        try:
            await self.transport.send_message(chat_id, greeting)
            dlg = dialog or await self.engine.db.get_or_create_dialog("avito", chat_id)
            await self.engine.db.add_message(dlg["id"], "assistant", greeting)
            logger.info("Proactive greeting sent for system event in chat %s", chat_id)
        except Exception:
            logger.warning("Failed to send greeting for system event in chat %s", chat_id, exc_info=True)
        return True

    async def _maybe_inject_item_context(
        self, incoming: IncomingMessage, body: dict
    ) -> IncomingMessage:
        """
        On the first buyer message about a specific listing, prepend item title
        and price so the LLM can immediately answer about the correct product.
        If item_id is absent/zero (general chat) or the dialog already has
        history, returns incoming unchanged.
        """
        if not (hasattr(self.transport, "_api") and hasattr(self.transport, "_user_id")):
            return incoming

        item_id = (body.get("payload", {}).get("value") or {}).get("item_id", 0)
        if not item_id:
            return incoming

        # Skip if the dialog already has message history (context already injected)
        dialog = await self.engine.db.get_dialog("avito", incoming.dialog_id)
        if dialog is not None:
            count = await self.engine.db.get_message_count(dialog["id"])
            if count > 0:
                return incoming

        try:
            response = await self.transport._api.get_chat(  # type: ignore[union-attr]
                self.transport._user_id, incoming.dialog_id  # type: ignore[union-attr]
            )
        except Exception:
            logger.warning("Could not fetch item context for chat %s", incoming.dialog_id)
            return incoming

        # Avito wraps the chat under a top-level "chat" key
        chat_root = response.get("chat", response)
        context_val = (chat_root.get("context") or {}).get("value") or {}
        title: str = context_val.get("title", "")
        price: int = (context_val.get("price") or {}).get("value", 0)

        if not title:
            return incoming

        if price:
            price_str = f"{price:,}".replace(",", " ")
            prefix = f"[Покупатель пишет по объявлению: «{title}», цена {price_str} ₽]"
        else:
            prefix = f"[Покупатель пишет по объявлению: «{title}»]"

        logger.info("Item context injected for chat %s: %s", incoming.dialog_id, prefix)
        return replace(incoming, text=f"{prefix}\n{incoming.text}")

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
