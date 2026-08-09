"""Dump raw messages from the price bot to see their actual format."""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_USERNAME = os.environ.get("PRICE_BOT_USERNAME", "")
SESSION = "price_monitor/session"
BUTTON_TEXT = "Полный прайс-лист"
TIMEOUT = 15.0


async def main() -> None:
    if not BOT_USERNAME:
        print("PRICE_BOT_USERNAME not set in .env")
        return

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Connected as {me.first_name}")

        collected = []
        async with client.conversation(BOT_USERNAME, timeout=TIMEOUT) as conv:
            await conv.send_message("/start")
            print("Sent /start")

            greeting = await conv.get_response(timeout=TIMEOUT)
            print(f"\n--- Greeting message ---\n{greeting.text}")
            if greeting.reply_markup:
                print(f"[has reply_markup: {type(greeting.reply_markup).__name__}]")
            collected.append(greeting.text or "")

            await conv.send_message(BUTTON_TEXT)
            print(f"\nSent button: {BUTTON_TEXT!r}")

            while True:
                try:
                    msg = await conv.get_response(timeout=TIMEOUT)
                    collected.append(msg.text or "")
                    print(f"\n--- Response ({len(msg.text or '')} chars) ---\n{(msg.text or '')[:500]}")
                except asyncio.TimeoutError:
                    break

    out = "scripts/bot_response_dump.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n\n===MSG===\n\n".join(collected))
    print(f"\nFull dump written to {out}")
    print(f"Total messages: {len(collected)}")


asyncio.run(main())
