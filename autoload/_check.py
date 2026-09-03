import yaml, json

with open("data/avito_listings.yaml", encoding="utf-8") as f:
    data = yaml.safe_load(f)
listings = data["listings"]
print("Total YAML entries:", len(listings))

empty = [e for e in listings if not e.get("price_skus")]
print("Empty price_skus (skipped by feed_generator):", len(empty))
for e in empty:
    print(" ", e["ad_id"], str(e.get("title", ""))[:50])

ids = [e["ad_id"] for e in listings]
dupes = [x for x in ids if ids.count(x) > 1]
print("Duplicate ad_ids:", dupes)

new_entries = [e for e in listings if int(e["ad_id"]) > 100_000_000_000]
print("\nNew entries from catalog:", len(new_entries))
for e in new_entries:
    skus = e.get("price_skus") or []
    label = (skus[0] + "...") if skus else "STATIC-no-skus"
    print(" ", e["ad_id"], "[" + label + "]", str(e.get("title", ""))[:35])
