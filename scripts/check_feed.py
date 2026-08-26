#!/usr/bin/env python3
import sqlite3, yaml

conn = sqlite3.connect('prices.db')
db_skus = {r[0] for r in conn.execute('SELECT sku FROM prices WHERE available=1').fetchall()}
conn.close()
print(f'DB SKUs in prices.db: {len(db_skus)}')

with open('data/avito_listings.yaml', encoding='utf-8') as f:
    mappings = yaml.safe_load(f)['listings']

for m in mappings:
    skus_needed = m.get('price_skus') or []
    title = m['title']
    if not skus_needed:
        print(f'  [skip-no-skus] {title}')
        continue
    found = [s for s in skus_needed if s in db_skus]
    if not found:
        print(f'  [missing-in-db] {title} — first sku: {skus_needed[0]}')
