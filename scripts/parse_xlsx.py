#!/usr/bin/env python3
"""Parse Avito export XLSX, extract listing data for XML feed generator."""
import pandas as pd
import json

SKIP_COLS = {'AvitoId', 'Id', 'Title', 'Description', 'ImageUrls', 'Price', 'Category'}

xl = pd.ExcelFile('infoautodownload/195958555_2026-08-26T14_25_02Z.xlsx')

all_listings = []
for sh in xl.sheet_names:
    try:
        df = xl.parse(sh, header=1)
    except Exception:
        continue
    if 'AvitoId' not in df.columns:
        continue

    # Only keep rows with numeric AvitoId (real listings, not header/description rows)
    df = df[pd.to_numeric(df['AvitoId'], errors='coerce').notna()].copy()
    if df.empty:
        continue

    extra_cols = [c for c in df.columns if c not in SKIP_COLS]
    print(f'Sheet [{sh}] -> {len(df)} listings, extra cols: {extra_cols[:10]}')

    for _, row in df.iterrows():
        avito_id = int(float(str(row['AvitoId']).strip()))
        extra = {}
        for col in extra_cols:
            val = row.get(col)
            if pd.notna(val) and str(val).strip() not in ('', 'nan'):
                extra[col] = str(val).strip()

        all_listings.append({
            'avito_id':    avito_id,
            'internal_id': str(row.get('Id', '')).strip(),
            'title':       str(row.get('Title', '')).strip(),
            'price':       row.get('Price', ''),
            'category':    str(row.get('Category', '')).strip(),
            'image_urls':  str(row.get('ImageUrls', '')).strip(),
            'description': str(row.get('Description', '')).strip(),
            'sheet':       sh,
            'extra_fields': extra,
        })
        print(f'  {avito_id}  |  {str(row.get("Title", ""))[:55]}')

print(f'\nTotal listings: {len(all_listings)}')

with open('infoautodownload/listings_data.json', 'w', encoding='utf-8') as f:
    json.dump(all_listings, f, ensure_ascii=False, indent=2)
print('Saved to infoautodownload/listings_data.json')
