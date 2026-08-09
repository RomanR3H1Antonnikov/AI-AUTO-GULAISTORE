import json
import logging
from datetime import datetime
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS dialogs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transport       TEXT    NOT NULL,
    external_id     TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'bot_active',
    takeover_type   TEXT,
    notified_at     TIMESTAMP,
    last_notif_type TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(transport, external_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_id   INTEGER NOT NULL REFERENCES dialogs(id),
    role        TEXT    NOT NULL,   -- 'user' | 'assistant'
    text        TEXT    NOT NULL,
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_id   INTEGER NOT NULL REFERENCES dialogs(id),
    type        TEXT    NOT NULL,   -- 'lead' | 'toxicity' | 'escalation' | 'token_alert'
    metadata    TEXT,               -- JSON blob
    sent_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS token_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_id           INTEGER NOT NULL REFERENCES dialogs(id),
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    date                DATE    DEFAULT (date('now')),
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS escalation_relay (
    tg_msg_id   INTEGER PRIMARY KEY,
    dialog_id   INTEGER NOT NULL REFERENCES dialogs(id),
    transport   TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    context     TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Dialogs ──────────────────────────────────────────────────────────────

    async def get_or_create_dialog(self, transport: str, external_id: str) -> dict:
        async with self._db.execute(
            "SELECT * FROM dialogs WHERE transport=? AND external_id=?",
            (transport, external_id),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)

        async with self._db.execute(
            "INSERT INTO dialogs (transport, external_id) VALUES (?,?)",
            (transport, external_id),
        ) as cur:
            new_id = cur.lastrowid
        await self._db.commit()

        async with self._db.execute("SELECT * FROM dialogs WHERE id=?", (new_id,)) as cur:
            return dict(await cur.fetchone())

    async def get_dialog(self, transport: str, external_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM dialogs WHERE transport=? AND external_id=?",
            (transport, external_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_dialog_by_id(self, dialog_id: int) -> Optional[dict]:
        async with self._db.execute("SELECT * FROM dialogs WHERE id=?", (dialog_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_dialog_status(
        self, dialog_id: int, status: str, takeover_type: Optional[str] = None
    ) -> None:
        await self._db.execute(
            """UPDATE dialogs
               SET status=?, takeover_type=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (status, takeover_type, dialog_id),
        )
        await self._db.commit()
        logger.info("dialog %d → status=%s takeover=%s", dialog_id, status, takeover_type)

    async def list_dialogs(self, status: Optional[str] = None, limit: int = 20) -> list[dict]:
        if status:
            q, p = "SELECT * FROM dialogs WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)
        else:
            q, p = "SELECT * FROM dialogs ORDER BY updated_at DESC LIMIT ?", (limit,)
        async with self._db.execute(q, p) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Messages ─────────────────────────────────────────────────────────────

    async def add_message(self, dialog_id: int, role: str, text: str) -> None:
        await self._db.execute(
            "INSERT INTO messages (dialog_id, role, text) VALUES (?,?,?)",
            (dialog_id, role, text),
        )
        await self._db.commit()

    async def get_messages(self, dialog_id: int, limit: int = 50) -> list[dict]:
        """Returns messages in chronological order (oldest first)."""
        async with self._db.execute(
            """SELECT * FROM (
                   SELECT * FROM messages WHERE dialog_id=? ORDER BY ts DESC LIMIT ?
               ) ORDER BY ts ASC""",
            (dialog_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_message_count(self, dialog_id: int) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE dialog_id=?", (dialog_id,)
        ) as cur:
            return (await cur.fetchone())[0]

    # ── Notifications ─────────────────────────────────────────────────────────

    async def record_notification(
        self, dialog_id: int, notif_type: str, metadata: Optional[dict] = None
    ) -> None:
        await self._db.execute(
            "INSERT INTO notifications (dialog_id, type, metadata) VALUES (?,?,?)",
            (dialog_id, notif_type, json.dumps(metadata) if metadata else None),
        )
        await self._db.execute(
            """UPDATE dialogs
               SET notified_at=CURRENT_TIMESTAMP, last_notif_type=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (notif_type, dialog_id),
        )
        await self._db.commit()

    async def get_last_notification(
        self, dialog_id: int, notif_type: Optional[str] = None
    ) -> Optional[dict]:
        if notif_type:
            q = "SELECT * FROM notifications WHERE dialog_id=? AND type=? ORDER BY sent_at DESC LIMIT 1"
            p = (dialog_id, notif_type)
        else:
            q = "SELECT * FROM notifications WHERE dialog_id=? ORDER BY sent_at DESC LIMIT 1"
            p = (dialog_id,)
        async with self._db.execute(q, p) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ── Token usage ──────────────────────────────────────────────────────────

    async def record_token_usage(
        self, dialog_id: int, prompt_tokens: int, completion_tokens: int
    ) -> None:
        total = prompt_tokens + completion_tokens
        await self._db.execute(
            """INSERT INTO token_usage (dialog_id, prompt_tokens, completion_tokens, total_tokens)
               VALUES (?,?,?,?)""",
            (dialog_id, prompt_tokens, completion_tokens, total),
        )
        await self._db.commit()

    async def get_dialogs_for_retention(self, silence_minutes: int = 5) -> list[dict]:
        """
        Return bot_active dialogs where:
        - The last message is from the assistant (bot replied, buyer silent)
        - That message is older than silence_minutes
        - No 'retention' notification was sent since that last message
        """
        query = """
        SELECT d.id, d.external_id, d.transport
        FROM dialogs d
        WHERE d.status = 'bot_active'
          AND EXISTS (
              SELECT 1 FROM messages m
              WHERE m.dialog_id = d.id
                AND m.role = 'assistant'
                AND m.ts <= datetime('now', ?)
                AND m.ts = (SELECT MAX(ts) FROM messages WHERE dialog_id = d.id)
                AND NOT EXISTS (
                    SELECT 1 FROM notifications n
                    WHERE n.dialog_id = d.id
                      AND n.type = 'retention'
                      AND n.sent_at >= m.ts
                )
          )
        """
        interval = f"-{silence_minutes} minutes"
        async with self._db.execute(query, (interval,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Escalation relay ─────────────────────────────────────────────────────

    async def store_escalation_relay(
        self,
        tg_msg_id: int,
        dialog_id: int,
        transport: str,
        external_id: str,
        context: str,
    ) -> None:
        await self._db.execute(
            """INSERT OR REPLACE INTO escalation_relay
               (tg_msg_id, dialog_id, transport, external_id, context)
               VALUES (?,?,?,?,?)""",
            (tg_msg_id, dialog_id, transport, external_id, context),
        )
        await self._db.commit()

    async def get_escalation_relay(self, tg_msg_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM escalation_relay WHERE tg_msg_id=?", (tg_msg_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_escalation_relay(self, tg_msg_id: int) -> None:
        await self._db.execute(
            "DELETE FROM escalation_relay WHERE tg_msg_id=?", (tg_msg_id,)
        )
        await self._db.commit()

    # ── Token usage ──────────────────────────────────────────────────────────

    async def get_daily_tokens(self, dialog_id: Optional[int] = None) -> int:
        """Total tokens consumed today, optionally filtered to one dialog."""
        if dialog_id is not None:
            q = "SELECT COALESCE(SUM(total_tokens),0) FROM token_usage WHERE dialog_id=? AND date=date('now')"
            p: tuple[Any, ...] = (dialog_id,)
        else:
            q = "SELECT COALESCE(SUM(total_tokens),0) FROM token_usage WHERE date=date('now')"
            p = ()
        async with self._db.execute(q, p) as cur:
            return (await cur.fetchone())[0]
