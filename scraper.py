"""
scraper.py — Shopee Indonesia Product Scraper
=============================================
Section 3 of the DE Analytics Portfolio (Kredivo Technical Test).

Target site: Shopee Indonesia (https://shopee.co.id)

Technical approach
------------------
Major Indonesian e-commerce platforms (Shopee, Blibli, Tokopedia) use TLS
fingerprinting to distinguish Python's requests library from a real browser —
the SSL/TLS handshake itself reveals the client identity before any headers
are read. Plain requests.get() always fails with 403 on these sites.

The solution is curl_cffi, which wraps libcurl and impersonates Chrome's
exact TLS fingerprint (cipher suites, extensions, elliptic curves). From the
server's perspective the connection is indistinguishable from Chrome 124.

With TLS impersonation in place, Shopee's internal JSON search API is
accessible with a single GET call — no Selenium, no headless browser,
no credentials required.

Ethical compliance
------------------
- Respects robots.txt: the /api/ search path is not disallowed on shopee.co.id
- Adds random delays between every request (see DELAY_BETWEEN_REQUESTS)
- No credentials, session tokens, or login are used
- No personal or user-generated private data is collected

Mandatory output fields
-----------------------
  product_name  : Item title as listed on Shopee
  category_id   : Shopee's numeric category ID (catid)
  price_idr     : Listed price in Indonesian Rupiah

Extra fields — each carries a short business justification
----------------------------------------------------------
  rating          : Average star rating -> demand signal; input to price
                    elasticity and recommendation-ranking models
  review_count    : Total reviews -> popularity proxy; weights rating
                    reliability (4.9 from 3 reviews != 4.9 from 3,000)
  units_sold      : Cumulative units sold -> sales velocity; critical input
                    for GMV modelling and inventory forecasting
  stock           : Current stock level -> live inventory intelligence
  seller_location : Seller city/region -> geographic supply mapping
  discount_pct    : Active discount -> promo strategy and markdown analysis
  product_url     : Canonical URL -> traceability and price monitoring

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
from typing import List, Optional
from urllib.parse import urlencode

from curl_cffi import requests  # replaces standard requests; impersonates Chrome TLS

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL     = "https://shopee.co.id/api/v4/search/search_items/"
REFERER_BASE = "https://shopee.co.id/search"

# User agent pool — must match the impersonation target (Chrome 124)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

KEYWORDS = [
    "laptop",
    "smartphone",
    "sepatu",       # shoes
    "skincare",
    "baju pria",    # men's clothing
]

ITEMS_PER_PAGE         = 60
PAGES_PER_KEYWORD      = 3           # 3 x 60 = 180 items/keyword -> ~900 total
DELAY_BETWEEN_REQUESTS = (1.5, 3.0)  # (min, max) seconds
REQUEST_TIMEOUT        = 20
MAX_RETRIES            = 3

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

    # Extra
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
    Return a curl_cffi Session that impersonates Chrome 124.

    impersonate="chrome124" sets the TLS cipher suites, extensions, and
    elliptic curves to exactly match Chrome 124 — making the connection
    indistinguishable from a real browser at the network level.
    """
    return requests.Session(impersonate="chrome124")


def build_headers(keyword: str) -> dict:
    """
    Request headers that match what Chrome sends to Shopee's search API.
    Combined with TLS impersonation, these make each request look like
    a genuine browser session.
    """
    return {
        "User-Agent"     : random.choice(USER_AGENTS),
        "Referer"        : f"{REFERER_BASE}?keyword={keyword}",
        "X-API-Source"   : "pc",
        "Accept"         : "application/json",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def build_url(keyword: str, offset: int) -> str:
    """
    Construct the Shopee search API URL.
    'newest' is the pagination offset: page 1=0, page 2=60, page 3=120.
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
    Shopee stores prices as integers scaled by 100,000.
    Example: 15,000,000 raw -> IDR 150,000.
    """
    return raw / 100_000


def parse_item(raw: dict) -> Optional[Product]:
    """
    Extract a Product from one item dict returned by the Shopee search API.
    Returns None if any mandatory field is missing.
    """
    try:
        basic = raw.get("item_basic", {})

        # --- Mandatory ---
        name  = basic.get("name", "").strip()
        catid = basic.get("catid")
        price = basic.get("price")

        if not name or catid is None or price is None:
            return None

        # --- Rating & reviews ---
        rating_block  = basic.get("item_rating", {})
        rating        = rating_block.get("rating_star")
        rating_counts = rating_block.get("rating_count", [])
        review_count  = rating_counts[0] if rating_counts else None

        # --- Sales & stock ---
        units_sold = basic.get("sold")
        stock      = basic.get("stock")

        # --- Seller location ---
        seller_location = basic.get("shop_location", "").strip() or None

        # --- Discount ---
        # Shopee may return discount as an integer (5) or a string ("-5%").
        # We normalise both to a plain positive integer percentage.
        discount_raw = basic.get("discount")
        discount_pct = None
        if discount_raw:
            cleaned = str(discount_raw).replace("%", "").replace("-", "").strip()
            try:
                discount_pct = int(cleaned) if cleaned else None
            except ValueError:
                discount_pct = None

        # --- URL ---
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
        log.warning("Could not parse item: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Core scraping logic
# ---------------------------------------------------------------------------

def scrape_keyword(session: requests.Session, keyword: str) -> List[Product]:
    """Paginate through PAGES_PER_KEYWORD pages for one keyword."""
    products: List[Product] = []

    for page in range(PAGES_PER_KEYWORD):
        offset = page * ITEMS_PER_PAGE
        url    = build_url(keyword, offset)

        log.info("  keyword=%-15r  page=%d/%d  offset=%d",
                 keyword, page + 1, PAGES_PER_KEYWORD, offset)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                t0       = time.time()
                resp     = session.get(url, headers=build_headers(keyword), timeout=REQUEST_TIMEOUT)
                duration = time.time() - t0
                resp.raise_for_status()
                break  # success — exit retry loop
            except Exception as exc:
                log.warning("  Attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s
                else:
                    log.error("  All retries exhausted for keyword=%r page=%d", keyword, page + 1)
                    return products  # return what we have so far

        log.debug("  HTTP %d in %.2f s", resp.status_code, duration)

        data  = resp.json()
        items = data.get("items") or []

        if not items:
            log.info("  No more items — stopping early for %r", keyword)
            break

        parsed = [parse_item(item) for item in items]
        valid  = [p for p in parsed if p is not None]
        products.extend(valid)
        log.info("  -> %d/%d items accepted", len(valid), len(items))

        delay = random.uniform(*DELAY_BETWEEN_REQUESTS)
        time.sleep(delay)

    return products


def warm_up_session(session: requests.Session) -> None:
    """
    Visit Shopee's homepage to acquire session cookies before hitting the API.

    Shopee checks that cookies from the main site are present on API calls.
    A fresh session (new TLS handshake) combined with a homepage visit makes
    each keyword's requests look like a brand-new browser opening the site.
    """
    try:
        session.get(
            "https://shopee.co.id/",
            headers={
                "User-Agent"     : random.choice(USER_AGENTS),
                "Accept"         : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            timeout=REQUEST_TIMEOUT,
        )
        log.debug("  Session warm-up complete")
        time.sleep(random.uniform(1.5, 2.5))
    except Exception as exc:
        log.warning("  Warm-up failed (continuing anyway): %s", exc)


def scrape_all(keywords: List[str]) -> List[Product]:
    """
    Scrape all keywords and return deduplicated products.

    A fresh session (new TLS handshake + new cookies) is created for each
    keyword. This resets Shopee's per-session rate-limit counter and makes
    each keyword's traffic look like a new browser visit.
    Deduplication uses product_url so the same listing is never counted twice.
    """
    all_products : List[Product] = []
    seen         : set           = set()

    for i, keyword in enumerate(keywords):
        log.info("--- Keyword: %r", keyword)

        # Fresh session per keyword — resets TLS fingerprint and cookies
        session = build_session()
        warm_up_session(session)

        for product in scrape_keyword(session, keyword):
            uid = product.product_url or product.product_name
            if uid not in seen:
                seen.add(uid)
                all_products.append(product)

        # Longer pause between keywords so the next session doesn't look
        # like it immediately follows the previous one from the same IP
        if i < len(keywords) - 1:
            pause = random.uniform(8.0, 15.0)
            log.info("  Pausing %.1f s before next keyword ...", pause)
            time.sleep(pause)

    log.info("Total unique products collected: %d", len(all_products))
    return all_products


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "product_name", "category_id", "price_idr",
    "rating", "review_count", "units_sold",
    "stock", "seller_location", "discount_pct", "product_url",
]


def save_csv(products: List[Product], path: str) -> None:
    """Write products to a UTF-8 CSV file with a header row."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(asdict(p) for p in products)
    log.info("CSV  -> %s  (%d rows)", path, len(products))


def save_json(products: List[Product], path: str) -> None:
    """Write products to a JSON file as an array of objects."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)
    log.info("JSON -> %s  (%d records)", path, len(products))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Shopee Indonesia Product Scraper — starting")
    log.info("Keywords : %s", KEYWORDS)
    log.info("Scope    : %d pages x %d items x %d keywords (max ~%d records)",
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
