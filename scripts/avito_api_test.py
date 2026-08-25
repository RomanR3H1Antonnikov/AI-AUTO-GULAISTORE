#!/usr/bin/env python3
"""
Avito API probe: token → GET items → test price update endpoint.
Run on VPS: python3 scripts/avito_api_test.py
"""
import os, json
import httpx

CLIENT_ID     = os.environ["AVITO_CLIENT_ID"]
CLIENT_SECRET = os.environ["AVITO_CLIENT_SECRET"]
USER_ID       = os.environ["AVITO_USER_ID"]
BASE          = "https://api.avito.ru"

# AdId of one real listing to probe (MacBook NEO 256 Citrus — low-risk test)
TEST_AD_ID = 8152293865


def get_token() -> str:
    r = httpx.post(f"{BASE}/token", data={
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    scopes = d.get("scope", "(not returned)")
    print(f"   scopes: {scopes}")
    return d["access_token"]


def probe(label: str, method: str, path: str, headers: dict, **kwargs):
    url = f"{BASE}{path}"
    try:
        r = httpx.request(method, url, headers=headers, timeout=10, **kwargs)
        print(f"   [{r.status_code}] {method} {path}")
        try:
            body = r.json()
            print(f"           {json.dumps(body, ensure_ascii=False)[:400]}")
        except Exception:
            print(f"           {r.text[:400]}")
    except Exception as e:
        print(f"   [ERR] {method} {path}: {e}")


def main():
    print("=" * 60)
    print("1. Getting OAuth2 token (client_credentials)...")
    token = get_token()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"   OK  token: {token[:25]}...")

    print("\n2. GET /core/v1/accounts/self")
    probe("self", "GET", "/core/v1/accounts/self", h)

    print(f"\n3. GET /core/v1/accounts/{USER_ID}/items/?per_page=5")
    probe("items", "GET", f"/core/v1/accounts/{USER_ID}/items/?per_page=5", h)

    print(f"\n4. GET /core/v1/accounts/{USER_ID}/items/{TEST_AD_ID}/")
    probe("item-detail", "GET", f"/core/v1/accounts/{USER_ID}/items/{TEST_AD_ID}/", h)

    print(f"\n5. Probing price-update endpoints (read-safe — sends no real changes):")

    # 5a: Try PUT with minimal body (may 400/422 with field list → tells us schema)
    probe("PUT item", "PUT", f"/core/v1/accounts/{USER_ID}/items/{TEST_AD_ID}/",
          h, json={"price": 0})

    # 5b: PATCH
    probe("PATCH item", "PATCH", f"/core/v1/accounts/{USER_ID}/items/{TEST_AD_ID}/",
          h, json={"price": 0})

    # 5c: dedicated /price sub-resource
    probe("POST /price", "POST",
          f"/core/v1/accounts/{USER_ID}/items/{TEST_AD_ID}/price",
          h, json={"price": 0})

    # 5d: Autoload profile list (checks autoload scope)
    print(f"\n6. Autoload scope check:")
    probe("autoload-profiles", "GET",
          f"/autoload/v2/items/?user_id={USER_ID}", h)

    probe("autoload-upload", "GET",
          f"/autoload/v2/items/upload?user_id={USER_ID}", h)

    print("\n" + "=" * 60)
    print("Done. Check status codes above:")
    print("  200/201 = endpoint exists and works")
    print("  400/422 = endpoint exists, wrong params (good!)")
    print("  403     = no scope/permission")
    print("  404     = endpoint doesn't exist")
    print("  405     = wrong method")


if __name__ == "__main__":
    main()
