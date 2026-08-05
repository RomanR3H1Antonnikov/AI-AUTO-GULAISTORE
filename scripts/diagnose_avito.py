"""
Avito API diagnostic — run on VPS:
  python3 scripts/diagnose_avito.py

Checks:
  1. Token acquisition
  2. Account info (self)
  3. Webhook subscriptions list
  4. Chats list (most recent 5)
"""

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["AVITO_CLIENT_ID"]
CLIENT_SECRET = os.environ["AVITO_CLIENT_SECRET"]
USER_ID       = int(os.environ.get("AVITO_USER_ID", "0"))
BASE          = "https://api.avito.ru"


async def get_token(session: aiohttp.ClientSession) -> str:
    async with session.post(
        "https://api.avito.ru/token",
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    ) as r:
        d = await r.json(content_type=None)
        if r.status != 200:
            print(f"[ERROR] Token: HTTP {r.status} → {d}")
            sys.exit(1)
        print(f"[OK] Token acquired (expires_in={d.get('expires_in')}s)")
        return d["access_token"]


async def get(session, token, path):
    async with session.get(
        BASE + path,
        headers={"Authorization": f"Bearer {token}"},
    ) as r:
        return r.status, await r.json(content_type=None)


async def post(session, token, path, body=None):
    async with session.post(
        BASE + path,
        headers={"Authorization": f"Bearer {token}"},
        json=body or {},
    ) as r:
        return r.status, await r.json(content_type=None)


async def main():
    async with aiohttp.ClientSession() as session:
        token = await get_token(session)

        # 1. Account self
        status, data = await get(session, token, "/core/v1/accounts/self")
        if status == 200:
            print(f"[OK] Account: id={data.get('id')} name={data.get('name')} email={data.get('email')}")
        else:
            print(f"[ERROR] /accounts/self HTTP {status}: {data}")

        # 2. Webhook subscriptions
        status, data = await post(session, token, "/messenger/v1/subscriptions")
        subs = data.get("subscriptions", []) if status == 200 else []
        if status == 200:
            if subs:
                print(f"[OK] Webhook subscriptions ({len(subs)}):")
                for s in subs:
                    print(f"       url={s.get('url')} version={s.get('version')}")
            else:
                print("[WARN] No webhook subscriptions found!")
        else:
            print(f"[ERROR] /subscriptions HTTP {status}: {data}")

        # 3. Chats list
        uid = USER_ID or data.get("id", 0)
        status, data = await get(session, token, f"/messenger/v2/accounts/{uid}/chats?limit=5")
        if status == 200:
            chats = data.get("chats", [])
            print(f"[OK] Chats visible via API: {len(chats)}")
            for c in chats[:5]:
                last = (c.get("last_message") or {}).get("content", {}).get("text", "—")
                print(f"       chat_id={c.get('id')}  last='{last[:60]}'")
        else:
            print(f"[ERROR] /chats HTTP {status}: {data}")


asyncio.run(main())
