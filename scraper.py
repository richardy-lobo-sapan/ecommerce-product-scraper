"""
scraper.py — Shopee Indonesia Product Scraper
=============================================
Section 3 of the DE Analytics Portfolio (Kredivo Technical Test).

Strategy
--------
Shopee's frontend is a SPA that fetches product data from its own internal
JSON search API. We call the same endpoint the browser calls, so no Selenium
or HTML parsing is required. This keeps the code clean, modular, and fast.

Ethical compliance
------------------
- Respects robots.txt: the /api/ search path is not disallowed on shopee.co.id
- Adds random delays between every request (see DELAY_BETWEEN_REQUESTS)
- Uses exponential-backoff retry — backs off further on 429/5xx responses
- No credentials, session tokens, or login are used
- No personal or user-generated private data is collected

Mandatory output fields
-----------------------
  product_name  : Item title as listed on Shopee
  category_id   : Shopee's numeric category ID (catid)
  price_idr     : Price in Indonesian Rupiah (converted from Shopee's raw int)

Extra fields — each carries a short business justification
----------------------------------------------------------
  rating          : Average star rating → demand signal; input to price
                    elasticity and recommendation-ranking models
  review_count    : Total review count → popularity proxy; used to weight
                    rating reliability (a 4.9 with 3 reviews ≠ 4.9 with 3 000)
  units_sold      : Cumulative units sold → sales velocity; critical input
                    for GMV modelling and inventory forecasting
  stock           : Current stock level → live inventory intelligence;
                    triggers low-stock alerts in supply chain pipelines
  seller_location : Seller's city/region → geographic supply mapping;
                    used in logistics cost analysis and regional demand studies
  discount_pct    : Active discount percentage → promotional strategy signal;
                    input to markdown optimisation and promo attribution models
  product_url     : Canonical listing URL → full traceability back to source;
                    enables price monitoring and deduplication across runs

Usage
-----
    pip install -r requirements.txt
    python scraper.py

Outputs:  result.csv  and  result.json  in the working directory.
"""

import csv
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration  (edit these to control scope and output)
# ---------------------------------------------------------------------------

BASE_URL     = "https://shopee.co.id/api/v4/search/search_items/"
REFERER_BASE = "https://shopee.co.id/search"

# User agent pool — rotated per session to reduce fingerprinting risk.
# Adapted from nerufuyo/neru-scrapper; limited to modern Chrome/Firefox on
# Windows and macOS (the most common profiles on Indonesian e-commerce sites).
USER_AGENTS = [
    # Chrome 124 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 123 — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox 125 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox 124 — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Edge 124 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

# Keywords to scrape — chosen to cover diverse categories for a richer dataset
KEYWORDS = [
    "laptop",
    "smartphone",
    "sepatu",        # shoes
    "skincare",
    "baju pria",     # men's clothing
]

ITEMS_PER_PAGE        = 60          # Shopee's per-request maximum
PAGES_PER_KEYWORD     = 3           # 3 × 60 = 180 items/keyword → ~900 total
DELAY_BETWEEN_REQUESTS = (1.5, 3.0) # (min, max) seconds — random jitter
REQUEST_TIMEOUT       = 15          # seconds per request
MAX_RETRIES           = 3           # retried on 429 / 5xx before giving up

OUTPUT_CSV  = "result.csv"
OUTPUT_JSON = "result.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Product:
    """One product row. Mirrors the columns in result.csv / result.json."""

    # Mandatory
    product_name : str
    category_id  : int
    price_idr    : float

    # Extra (all Optional — Shopee may omit any of these for some listings)
    rating          : Optional[float] = None
    review_count    : Optional[int]   = None
    units_sold      : Optional[int]   = None
    stock           : Optional[int]   = None
    seller_location : Optional[str]   = None
    discount_pct    : Optional[int]   = None
    product_url     : Optional[str]   = None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """
    Return a requests.Session pre-configured with a retry adapter.

    Retries on 429 (rate-limited) and all 5xx server errors using
    exponential backoff (2 s, 4 s, 8 s …) so we don't hammer a struggling
    server — aligns with ethical scraping and avoids IP bans.
    """
    session = requests.Session()
    retry = Retry(
        total             = MAX_RETRIES,
        backoff_factor    = 2,
        status_forcelist  = [429, 500, 502, 503, 504],
        allowed_methods   = ["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def build_headers(keyword: str) -> dict:
    """
    Mimic the headers a browser sends when hitting the Shopee search API.

    UA is rotated from USER_AGENTS on each call (adapted from nerufuyo/neru-scrapper)
    to reduce fingerprinting risk. Accept-Language is set to id-ID so the server
    returns Indonesian-locale responses, matching what a real user would send.
    No auth tokens or cookies are included — this endpoint is fully public.
    """
    return {
        "User-Agent"     : random.choice(USER_AGENTS),
        "Referer"        : f"{REFERER_BASE}?keyword={keyword}",
        "X-API-Source"   : "pc",
        "Accept"         : "application/json",
        # Prefer Indonesian locale — makes requests look more authentic and
        # ensures prices/text are returned in the expected regional format.
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def build_url(keyword: str, offset: int) -> str:
    """
    Construct the Shopee search API URL.

    'newest' is Shopee's offset parameter — it controls pagination.
    Page 1 → newest=0, page 2 → newest=60, page 3 → newest=120, …
    """
    params = {
        "by"       : "relevance",
        "keyword"  : keyword,
        "limit"    : ITEMS_PER_PAGE,
        "newest"   : offset,
        "order"    : "desc",
        "page_type": "search",
        "scenario" : "PAGE_GLOBAL_SEARCH",
        "version"  : 2,
    }
    return f"{BASE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_price(raw: int) -> float:
    """
    Shopee stores prices as integers scaled by 100 000.
    Example: 15_000_000 raw → IDR 150 000 actual.
    """
    return raw / 100_000


def parse_item(raw: dict) -> Optional[Product]:
    """
    Extract a Product from one item dict returned by the Shopee search API.

    Returns None (and logs a warning) if any mandatory field is absent,
    so the caller can safely skip bad rows without crashing the run.
    """
    try:
        basic = raw.get("item_basic", {})

        # --- Mandatory fields ---
        name  = basic.get("name", "").strip()
        catid = basic.get("catid")
        price = basic.get("price")

        if not name or catid is None or price is None:
            log.debug("Skipping item with missing mandatory field: %s", basic.get("itemid"))
            return None

        # --- Rating & reviews ---
        rating_block  = basic.get("item_rating", {})
        rating        = rating_block.get("rating_star")
        # rating_count is an array: [total, 5★, 4★, 3★, 2★, 1★]
        rating_counts = rating_block.get("rating_count", [])
        review_count  = rating_counts[0] if rating_counts else None

        # --- Sales & stock ---
        units_sold = basic.get("sold")
        stock      = basic.get("stock")

        # --- Seller location (string like "Jakarta Pusat", "Surabaya") ---
        seller_location = basic.get("shop_location", "").strip() or None

        # --- Discount percentage (None means no active promo) ---
        discount_raw = basic.get("discount")
        discount_pct = int(discount_raw) if discount_raw else None

        # --- Canonical URL built from shopid + itemid ---
        shopid = basic.get("shopid")
        itemid = basic.get("itemid")
        product_url = (
            f"https://shopee.co.id/product/{shopid}/{itemid}"
            if shopid and itemid else None
        )

        return Product(
            product_name    = name,
            category_id     = int(catid),
            price_idr       = parse_price(price),
            rating          = round(float(rating), 2) if rating else None,
            review_count    = int(review_count) if review_count is not None else None,
            units_sold      = int(units_sold) if units_sold is not None else None,
            stock           = int(stock) if stock is not None else None,
            seller_location = seller_location,
            discount_pct    = discount_pct,
            product_url     = product_url,
        )

    except (TypeError, ValueError, KeyError) as exc:
        log.warning("Could not parse item — %s: %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def scrape_keyword(session: requests.Session, keyword: str) -> list[Product]:
    """
    Paginate through PAGES_PER_KEYWORD pages for one keyword.

    Stops early if a page returns zero items (meaning we've exhausted results).
    On a persistent request failure, logs the error and returns what we have.
    """
    products: list[Product] = []

    for page in range(PAGES_PER_KEYWORD):
        offset = page * ITEMS_PER_PAGE
        url    = build_url(keyword, offset)

        log.info("  keyword=%-15r  page=%d/%d  offset=%d",
                 keyword, page + 1, PAGES_PER_KEYWORD, offset)

        try:
            t0   = time.time()
            resp = session.get(url, headers=build_headers(keyword), timeout=REQUEST_TIMEOUT)
            duration = time.time() - t0
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Request failed (keyword=%r, page=%d): %s", keyword, page + 1, exc)
            break  # skip remaining pages for this keyword

        # Log response time — a sudden increase (e.g. >5 s) signals rate-limiting
        log.debug("  Response: HTTP %d in %.2f s", resp.status_code, duration)

        data  = resp.json()
        items = data.get("items") or []

        if not items:
            log.info("  No more items at offset=%d — stopping early for %r", offset, keyword)
            break

        parsed = [parse_item(item) for item in items]
        valid  = [p for p in parsed if p is not None]
        products.extend(valid)
        log.info("  → %d/%d items accepted", len(valid), len(items))

        # Polite delay — random jitter avoids a predictable request pattern
        delay = random.uniform(*DELAY_BETWEEN_REQUESTS)
        log.debug("  Sleeping %.1f s …", delay)
        time.sleep(delay)

    return products


def scrape_all(keywords: list[str]) -> list[Product]:
    """
    Run the full scrape across all keywords.

    Deduplicates by product URL (or product name as fallback) so a listing
    that appears under multiple keyword searches is only kept once.
    """
    session      = build_session()
    all_products : list[Product] = []
    seen         : set[str]      = set()

    for keyword in keywords:
        log.info("─── Keyword: %r", keyword)
        for product in scrape_keyword(session, keyword):
            uid = product.product_url or product.product_name
            if uid not in seen:
                seen.add(uid)
                all_products.append(product)

    log.info("Total unique products collected: %d", len(all_products))
    return all_products


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# Column order kept consistent across CSV and JSON
FIELDNAMES = [
    "product_name", "category_id", "price_idr",
    "rating", "review_count", "units_sold",
    "stock", "seller_location", "discount_pct", "product_url",
]


def save_csv(products: list[Product], path: str) -> None:
    """Write products to a UTF-8 CSV file with a header row."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(asdict(p) for p in products)
    log.info("CSV  → %s  (%d rows)", path, len(products))


def save_json(products: list[Product], path: str) -> None:
    """Write products to a JSON file as an array of objects."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)
    log.info("JSON → %s  (%d records)", path, len(products))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Shopee Indonesia Product Scraper — starting")
    log.info("Keywords : %s", KEYWORDS)
    log.info("Scope    : %d pages × %d items × %d keywords (max ~%d records)",
             PAGES_PER_KEYWORD, ITEMS_PER_PAGE, len(KEYWORDS),
             PAGES_PER_KEYWORD * ITEMS_PER_PAGE * len(KEYWORDS))

    products = scrape_all(KEYWORDS)

    if not products:
        log.warning("No products collected. Check your connection or inspect the API response.")
        return

    save_csv(products, OUTPUT_CSV)
    save_json(products, OUTPUT_JSON)
    log.info("Done.")


if __name__ == "__main__":
    main()
