"""
One-shot script: fetch last 30 messages from PRICE_GROUP_ID and print them.
Used to inspect the real message format before tuning the parser.

Run:  python scripts/fetch_channel_prices.py
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID    = int(os.environ["TG_API_ID"])
API_HASH  = os.environ["TG_API_HASH"]
SESSION   = os.environ.get("TG_SESSION_PATH", "price_monitor/session")
GROUP_ID  = int(os.environ["PRICE_GROUP_ID"])
LIMIT     = int(os.environ.get("PRICE_GROUP_MSG_LIMIT", "30"))


OUTPUT = "scripts/channel_dump.txt"

async def main():
    lines = []
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        lines.append(f"Connected. Reading last {LIMIT} messages from {GROUP_ID}\n")
        lines.append("=" * 70 + "\n")
        async for msg in client.iter_messages(GROUP_ID, limit=LIMIT):
            if not msg.text:
                continue
            lines.append(f"--- msg_id={msg.id}  date={msg.date}  edited={msg.edit_date} ---\n")
            lines.append(msg.text + "\n\n")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"Done. See {OUTPUT}")

asyncio.run(main())
