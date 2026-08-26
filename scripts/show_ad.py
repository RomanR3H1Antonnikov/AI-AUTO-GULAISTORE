#!/usr/bin/env python3
import sys

avito_id = sys.argv[1] if len(sys.argv) > 1 else "8152293865"

with open('infoautodownload/test_feed.xml', encoding='utf-8') as f:
    xml = f.read()

marker = f'<AvitoId>{avito_id}</AvitoId>'
start = xml.find(marker)
if start == -1:
    print(f'AvitoId {avito_id} not found in feed')
    sys.exit(0)

ad_start = xml.rfind('<Ad>', 0, start)
ad_end = xml.find('</Ad>', start) + 5
print(xml[ad_start:ad_end])
