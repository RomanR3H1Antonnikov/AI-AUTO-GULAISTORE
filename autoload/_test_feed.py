"""Quick sanity check: run feed_generator and print stats."""
import sys, logging
from pathlib import Path
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(levelname)s %(message)s")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from autoload.feed_generator import generate_feed

xml = generate_feed(
    listings_json=str(ROOT / "infoautodownload" / "listings_data.json"),
    avito_yaml=str(ROOT / "data" / "avito_listings.yaml"),
    prices_db=str(ROOT / "prices.db"),
)

print(f"\nXML size: {len(xml):,} chars")
ad_count = xml.count("<Ad>")
print(f"<Ad> blocks in output: {ad_count}")
