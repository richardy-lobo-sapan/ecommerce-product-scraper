# =============================================================================
# scraper.py — Shopee Indonesia Product Scraper
# =============================================================================
# Author  : Richardy Lobo Sapan
# Project : DE Analytics Portfolio — Section 3 (Kredivo Technical Test)
# Target  : Shopee Indonesia (https://shopee.co.id)
# Output  : result.csv and result.json
# =============================================================================
#
# OVERVIEW
# --------
# Shopee is a Single Page Application (SPA). Instead of serving product data
# inside HTML, its frontend calls an internal JSON search API in the background.
# This scraper calls that same API endpoint directly — no HTML parsing needed.
#
# TECHNICAL CHALLENGE — TLS Fingerprinting
# -----------------------------------------
# Major Indonesian e-commerce platforms (Shopee, Tokopedia, Blibli) use a
# security technique called TLS fingerprinting. When your browser connects to
# a website, the SSL/TLS handshake reveals metadata about the client — things
# like which cipher suites and extensions it supports. Python's standard
# `requests` library has a different TLS signature than Chrome, so Shopee
# detects it as a bot and returns HTTP 403 Forbidden.
#
# SOLUTION — curl_cffi
# ---------------------
# We use `curl_cffi`, a library that wraps the system's libcurl and impersonates
# Chrome 124's exact TLS fingerprint. From Shopee's perspective, every request
# looks identical to a real Chrome browser connection.
#
# ETHICAL COMPLIANCE
# ------------------
# - The /api/ search path is not disallowed in Shopee's robots.txt
# - Random delays are added between every request (mimics human browsing pace)
# - No login credentials or private session tokens are used
# - No personal or user-generated private data is collected
#
# HOW TO RUN
# ----------
#   pip install -r requirements.txt
#   python scraper.py
#
# =============================================================================


# -----------------------------------------------------------------------------
# IMPORTS
# -----------------------------------------------------------------------------

import csv            # Built-in: for writing result.csv
import json           # Built-in: for writing result.json
import logging        # Built-in: for timestamped console output during the run
import random         # Built-in: for random delays (avoids predictable bot patterns)
import time           # Built-in: for sleep/delay between requests
from dataclasses import asdict, dataclass  # Built-in: clean data model + easy serialisation
from typing import List, Optional          # Built-in: type hints (Python 3.9 compatible)
from urllib.parse import urlencode         # Built-in: safely encodes URL query parameters

# curl_cffi replaces the standard `requests` library.
# It impersonates Chrome's TLS fingerprint, bypassing Shopee's bot detection.
# Install with: pip install curl_cffi
from curl_cffi import requests


# -----------------------------------------------------------------------------
# LOGGING SETUP
# -----------------------------------------------------------------------------
# Configures the logger to print timestamped messages to the console.
# Format example: "10:47:56  INFO  --- Keyword: 'laptop'"
# This lets us track progress and catch errors in real time.

logging.basicConfig(
    level   = logging.INFO,                          # Show INFO and above (INFO, WARNING, ERROR)
    format  = "%(asctime)s  %(levelname)s  %(message)s",
    datefmt = "%H:%M:%S",                            # Show time as HH:MM:SS only
)
log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
# All tuneable settings live here. Change these to control what gets scraped
# and how the scraper behaves — no need to touch any other part of the code.

# Shopee's internal search API endpoint.
# This is the same URL the browser calls when you search on shopee.co.id.
BASE_URL = "https://shopee.co.id/api/v4/search/search_items/"

# The Referer header tells Shopee's server which page the request came from.
# A real user would have been on the search page before the API was called.
REFERER_BASE = "https://shopee.co.id/search"

# Pool of User-Agent strings to rotate between requests.
# Rotating UAs makes individual requests harder to fingerprint as automated traffic.
# All strings match Chrome browsers (consistent with our TLS impersonation target).
USER_AGENTS = [
    # Chrome 124 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 123 on Windows (slightly older version for variety)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Search keywords — chosen to cover diverse product categories.
# Each keyword becomes one "topic" in the scraping run.
KEYWORDS = [
    "laptop",
    "smartphone",
    "sepatu",       # Indonesian for "shoes"
    "skincare",
    "baju pria",    # Indonesian for "men's clothing"
]

# Shopee returns a maximum of 60 items per API request.
ITEMS_PER_PAGE = 60

# How many pages to fetch per keyword.
# 3 pages × 60 items = 180 items per keyword → ~900 total across 5 keywords.
PAGES_PER_KEYWORD = 3

# Random delay range (in seconds) between page requests within one keyword.
# A random value between MIN and MAX is chosen each time to avoid a
# predictable, robotic request pattern that bot detectors look for.
DELAY_BETWEEN_REQUESTS = (1.5, 3.0)   # (minimum seconds, maximum seconds)

# Longer pause between keywords, giving Shopee's rate limiter time to reset.
# A fresh session is also created for each keyword (see scrape_all).
DELAY_BETWEEN_KEYWORDS = (8.0, 15.0)  # (minimum seconds, maximum seconds)

# How long to wait for a single HTTP request before giving up (in seconds).
REQUEST_TIMEOUT = 20

# How many times to retry a failed request before giving up on that page.
# Uses exponential backoff: waits 2s after attempt 1, 4s after attempt 2.
MAX_RETRIES = 3

# Output file paths — both are written to the current working directory.
OUTPUT_CSV  = "result.csv"
OUTPUT_JSON = "result.json"


# -----------------------------------------------------------------------------
# DATA MODEL
# -----------------------------------------------------------------------------

@dataclass
class Product:
    """
    Represents one product row in the output files.

    Using @dataclass gives us:
      - Auto-generated __init__ (no boilerplate constructor needed)
      - Type annotations on every field (self-documenting)
      - asdict() compatibility — converts directly to dict for CSV/JSON output

    Fields marked Optional can be None if Shopee doesn't return them
    for a particular listing (e.g. new products with no reviews yet).
    """

    # --- Mandatory fields (required by the Kredivo test spec) ---

    product_name : str    # Full product title as listed on Shopee
    category_id  : int    # Shopee's numeric category ID (e.g. 11044 = laptops)
    price_idr    : float  # Listed price in Indonesian Rupiah

    # --- Extra fields (each justified below for business value) ---

    rating : Optional[float] = None
    # Average star rating (0.0–5.0).
    # Business value: demand quality signal; used as input to price elasticity
    # models and recommendation ranking (higher-rated items get boosted).

    review_count : Optional[int] = None
    # Total number of customer reviews.
    # Business value: weights the reliability of `rating`. A product with 4.9
    # stars from 3 reviews is very different from one with 4.9 from 3,000.

    units_sold : Optional[int] = None
    # Cumulative units sold (Shopee's "terjual" field).
    # Business value: sales velocity proxy; critical input for GMV modelling,
    # inventory forecasting, and identifying trending products.

    stock : Optional[int] = None
    # Current stock level available for purchase.
    # Business value: live inventory intelligence; used to trigger low-stock
    # alerts in supply chain pipelines and measure sell-through rates.

    seller_location : Optional[str] = None
    # Seller's city or region (e.g. "Jakarta Pusat", "Surabaya").
    # Business value: geographic supply mapping; input to logistics cost
    # analysis and regional demand studies.

    discount_pct : Optional[int] = None
    # Active discount percentage (e.g. 15 means 15% off).
    # Business value: promotional strategy signal; input to markdown
    # optimisation models and promo attribution analysis.

    product_url : Optional[str] = None
    # Canonical listing URL (e.g. https://shopee.co.id/product/123/456).
    # Business value: full traceability back to the source listing; enables
    # price monitoring across multiple scrape runs and deduplication.


# -----------------------------------------------------------------------------
# HTTP SESSION HELPERS
# -----------------------------------------------------------------------------

def build_session() -> requests.Session:
    """
    Create and return a new curl_cffi HTTP session.

    The key parameter is impersonate="chrome124". This tells curl_cffi to
    configure the underlying libcurl with Chrome 124's exact TLS settings:
      - Cipher suite list (the encryption algorithms offered during handshake)
      - TLS extensions (e.g. SNI, ALPN, session tickets)
      - Elliptic curve preferences

    From Shopee's TLS inspection layer, this connection is indistinguishable
    from a real Chrome 124 browser — bypassing fingerprint-based 403 blocks.

    A new session is created for each keyword (see scrape_all) so each
    keyword batch starts with a completely fresh TLS handshake and clean
    cookie jar, resetting Shopee's per-session rate limiting.
    """
    return requests.Session(impersonate="chrome124")


def warm_up_session(session: requests.Session) -> None:
    """
    Visit Shopee's homepage before making any API calls.

    Why this is necessary:
    Shopee's API checks for cookies that are only set when the homepage is
    first loaded (e.g. session identifiers, CSRF tokens). A real user always
    visits shopee.co.id before searching — skipping this step makes the
    session look suspicious. This warm-up replicates natural browsing flow.

    The short sleep after mimics a human taking a moment before typing
    their search query.
    """
    log.info("  Warming up session (loading Shopee homepage) ...")
    try:
        session.get(
            "https://shopee.co.id/",
            headers={
                "User-Agent"     : random.choice(USER_AGENTS),
                "Accept"         : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer"        : "https://shopee.co.id/",
            },
            timeout=REQUEST_TIMEOUT,
        )
        log.info("  Warm-up complete — session cookies acquired")
        # Short human-like pause before starting API requests
        time.sleep(random.uniform(1.5, 2.5))
    except Exception as exc:
        # Non-fatal: log the failure and continue anyway
        log.warning("  Warm-up failed (will try API regardless): %s", exc)


def build_headers(keyword: str) -> dict:
    """
    Build the HTTP request headers for one API call.

    These headers mimic what Chrome sends when it calls Shopee's search API.
    Combined with TLS impersonation (in build_session), this makes the full
    request indistinguishable from a genuine browser session.

    Headers explained:
      User-Agent      : Identifies the browser. Rotated randomly from USER_AGENTS.
      Referer         : The page the user was on before this request. Shopee
                        expects this to be the search results page.
      X-API-Source    : Shopee's internal field indicating the client type.
                        "pc" = web browser (as opposed to "mobile" or "app").
      Accept          : Tells Shopee we want JSON back (not HTML).
      Accept-Language : Requests Indonesian-locale responses. Ensures prices
                        and text are in the correct regional format.
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
    Construct the full Shopee search API URL for one page of results.

    Shopee paginates using the 'newest' parameter (an offset, not a page number):
      Page 1 → newest=0   (items 1–60)
      Page 2 → newest=60  (items 61–120)
      Page 3 → newest=120 (items 121–180)

    Other parameters:
      by        : Sort criterion. "relevance" returns results Shopee considers
                  most relevant to the keyword.
      limit     : Items per request. 60 is Shopee's maximum.
      order     : Sort direction. "desc" = highest relevance first.
      page_type : Tells the API this is a search page request.
      scenario  : Identifies the UI context on Shopee's end.
      version   : API version number.
    """
    params = {
        "by"       : "relevance",
        "keyword"  : keyword,
        "limit"    : ITEMS_PER_PAGE,
        "newest"   : offset,          # Pagination offset (not page number)
        "order"    : "desc",
        "page_type": "search",
        "scenario" : "PAGE_GLOBAL_SEARCH",
        "version"  : 2,
    }
    # urlencode safely handles special characters in the keyword
    # e.g. "baju pria" becomes "baju+pria" in the URL
    return f"{BASE_URL}?{urlencode(params)}"


# -----------------------------------------------------------------------------
# DATA PARSING
# -----------------------------------------------------------------------------

def parse_price(raw_price: int) -> float:
    """
    Convert Shopee's raw price integer to a real IDR value.

    Shopee stores all prices as integers scaled by 100,000 to avoid
    floating-point precision issues in their database.

    Examples:
      Raw 7299000000 → IDR 72,990 (wrong — this would be if scaled by 100,000)
      Raw 729900000  → IDR 7,299,000 (a typical laptop price)

    The conversion is simply: real_price = raw_price / 100_000
    """
    return raw_price / 100_000


def parse_item(raw: dict) -> Optional[Product]:
    """
    Extract and validate one product from a raw Shopee API response dict.

    Shopee's API wraps each product's data inside an 'item_basic' key.
    We navigate into that object and extract each field carefully,
    handling missing data gracefully so one bad row never crashes the run.

    Returns a Product dataclass if all mandatory fields are present.
    Returns None if any mandatory field is missing — the caller skips None.
    """
    try:
        # All product fields are nested inside 'item_basic'
        basic = raw.get("item_basic", {})

        # --- Extract mandatory fields first ---
        # If any of these are missing, we cannot build a valid Product row.

        name  = basic.get("name", "").strip()  # Product title
        catid = basic.get("catid")              # Numeric category ID
        price = basic.get("price")              # Raw price integer (needs conversion)

        # Guard: skip this item entirely if mandatory fields are absent
        if not name or catid is None or price is None:
            log.debug("Skipping item — missing mandatory field (id: %s)", basic.get("itemid"))
            return None

        # --- Extract rating and review count ---
        # Shopee returns both inside a nested 'item_rating' object.
        rating_block  = basic.get("item_rating", {})
        rating        = rating_block.get("rating_star")   # Float, e.g. 4.88

        # 'rating_count' is a list: [total, 5-star, 4-star, 3-star, 2-star, 1-star]
        # Index 0 is the total review count, which is what we want.
        rating_counts = rating_block.get("rating_count", [])
        review_count  = rating_counts[0] if rating_counts else None

        # --- Extract sales and stock data ---
        units_sold = basic.get("sold")   # Cumulative units sold
        stock      = basic.get("stock")  # Current available stock

        # --- Extract seller location ---
        # Returns a string like "Jakarta Pusat" or "Surabaya"
        # 'or None' converts empty string "" to None for clean output
        seller_location = basic.get("shop_location", "").strip() or None

        # --- Extract and normalise discount percentage ---
        # Shopee is inconsistent: sometimes returns an integer (15),
        # sometimes a formatted string ("-15%"). We handle both cases.
        discount_raw = basic.get("discount")
        discount_pct = None  # Default to None (no discount)

        if discount_raw:
            # Strip the "%" sign and the "-" negative sign, then convert to int
            # Example: "-15%" → "15%" → "15" → 15
            cleaned = str(discount_raw).replace("%", "").replace("-", "").strip()
            try:
                discount_pct = int(cleaned) if cleaned else None
            except ValueError:
                discount_pct = None  # If conversion fails, treat as no discount

        # --- Build the canonical product URL ---
        # Shopee's product URLs follow the pattern:
        # https://shopee.co.id/product/{shopid}/{itemid}
        shopid = basic.get("shopid")
        itemid = basic.get("itemid")
        product_url = (
            f"https://shopee.co.id/product/{shopid}/{itemid}"
            if shopid and itemid else None
        )

        # --- Assemble and return the Product dataclass ---
        return Product(
            product_name    = name,
            category_id     = int(catid),
            price_idr       = parse_price(price),
            # Round rating to 2 decimal places; return None if not available
            rating          = round(float(rating), 2) if rating else None,
            review_count    = int(review_count) if review_count is not None else None,
            units_sold      = int(units_sold) if units_sold is not None else None,
            stock           = int(stock) if stock is not None else None,
            seller_location = seller_location,
            discount_pct    = discount_pct,
            product_url     = product_url,
        )

    except (TypeError, ValueError, KeyError) as exc:
        # Catch any unexpected parsing error for this item.
        # Log it as a warning and return None — the run continues.
        log.warning("Could not parse item (skipping): %s", exc)
        return None


# -----------------------------------------------------------------------------
# CORE SCRAPING LOGIC
# -----------------------------------------------------------------------------

def scrape_keyword(session: requests.Session, keyword: str) -> List[Product]:
    """
    Scrape all pages for a single keyword and return a list of Products.

    For each page:
      1. Build the API URL with the correct offset
      2. Send the HTTP request with Chrome-like headers
      3. Retry up to MAX_RETRIES times on failure (with exponential backoff)
      4. Parse each item in the JSON response
      5. Sleep before the next page (polite delay)

    Stops early if:
      - A page returns zero items (we've reached the end of results)
      - All retries are exhausted for a page (network issue)

    Returns whatever products were successfully collected, even if partial.
    """
    products: List[Product] = []  # Accumulates valid products across all pages

    for page in range(PAGES_PER_KEYWORD):

        # Calculate the offset for this page (0, 60, 120, ...)
        offset = page * ITEMS_PER_PAGE
        url    = build_url(keyword, offset)

        log.info("  keyword=%-15r  page=%d/%d  offset=%d",
                 keyword, page + 1, PAGES_PER_KEYWORD, offset)

        # --- Retry loop: attempt the request up to MAX_RETRIES times ---
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Record start time so we can log how long the request took
                t0   = time.time()
                resp = session.get(url, headers=build_headers(keyword), timeout=REQUEST_TIMEOUT)

                # Raise a Python exception for any 4xx or 5xx HTTP status code
                # (e.g. 403 Forbidden, 429 Too Many Requests, 500 Server Error)
                resp.raise_for_status()

                duration = time.time() - t0
                log.debug("  HTTP %d received in %.2f s", resp.status_code, duration)

                break  # Request succeeded — exit the retry loop

            except Exception as exc:
                log.warning("  Attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)

                if attempt < MAX_RETRIES:
                    # Exponential backoff: wait 2s before retry 2, 4s before retry 3.
                    # This gives the server time to recover and reduces our detection risk.
                    backoff = 2 ** attempt
                    log.info("  Waiting %ds before retry ...", backoff)
                    time.sleep(backoff)
                else:
                    # All retries exhausted — return whatever we have so far
                    log.error("  All retries exhausted for keyword=%r page=%d — skipping",
                               keyword, page + 1)
                    return products

        # --- Parse the JSON response ---
        data  = resp.json()
        items = data.get("items") or []  # 'items' is the array of product dicts

        # If the API returned an empty list, we've reached the end of results
        if not items:
            log.info("  No more items at offset=%d — stopping early for %r", offset, keyword)
            break

        # Parse each raw item dict into a Product dataclass (or None if invalid)
        parsed = [parse_item(item) for item in items]

        # Filter out None values (items that failed mandatory field validation)
        valid  = [p for p in parsed if p is not None]

        products.extend(valid)
        log.info("  -> %d/%d items accepted", len(valid), len(items))

        # --- Polite delay before the next page ---
        # Random jitter within the configured range so requests don't arrive
        # at a perfectly regular interval (a common bot signature).
        delay = random.uniform(*DELAY_BETWEEN_REQUESTS)
        log.debug("  Sleeping %.1f s before next page ...", delay)
        time.sleep(delay)

    return products


def scrape_all(keywords: List[str]) -> List[Product]:
    """
    Orchestrate the full scraping run across all keywords.

    For each keyword:
      1. Create a brand-new curl_cffi session (fresh TLS + clean cookies)
      2. Warm up the session by visiting Shopee's homepage
      3. Scrape all pages for that keyword
      4. Deduplicate against already-collected products
      5. Pause before moving to the next keyword

    Deduplication strategy:
      Uses product_url as the unique identifier. The same product can appear
      in results for multiple keywords (e.g. a laptop brand appears for both
      "laptop" and "asus laptop"). Without deduplication, it would be counted
      multiple times, skewing downstream analysis.
    """
    all_products : List[Product] = []  # Master list of all unique products
    seen         : set           = set()  # Set of seen product URLs (for dedup)

    for i, keyword in enumerate(keywords):
        log.info("--- Keyword: %r  (%d/%d)", keyword, i + 1, len(keywords))

        # Create a fresh session for each keyword.
        # This gives us a new TLS handshake and empty cookie jar, which resets
        # Shopee's per-session rate limiting counter.
        session = build_session()
        warm_up_session(session)

        # Scrape all pages for this keyword
        for product in scrape_keyword(session, keyword):

            # Use product_url as the deduplication key.
            # Fall back to product_name if URL is unavailable.
            uid = product.product_url or product.product_name

            if uid not in seen:
                seen.add(uid)
                all_products.append(product)

        # Pause between keywords (skip pause after the last keyword)
        if i < len(keywords) - 1:
            pause = random.uniform(*DELAY_BETWEEN_KEYWORDS)
            log.info("  Pausing %.1f s before next keyword ...", pause)
            time.sleep(pause)

    log.info("Total unique products collected: %d", len(all_products))
    return all_products


# -----------------------------------------------------------------------------
# OUTPUT — CSV AND JSON
# -----------------------------------------------------------------------------

# Column order for CSV headers and JSON field order.
# Defined once here so both save functions stay in sync.
FIELDNAMES = [
    "product_name",
    "category_id",
    "price_idr",
    "rating",
    "review_count",
    "units_sold",
    "stock",
    "seller_location",
    "discount_pct",
    "product_url",
]


def save_csv(products: List[Product], path: str) -> None:
    """
    Write the list of products to a UTF-8 encoded CSV file.

    Uses csv.DictWriter which writes rows from dictionaries.
    asdict(p) converts each Product dataclass to a plain dict automatically.
    encoding="utf-8" is required to correctly handle Indonesian characters
    in product names (e.g. accented characters, special symbols).
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()                           # Write the column header row
        writer.writerows(asdict(p) for p in products)  # Write one row per product
    log.info("CSV  -> %s  (%d rows written)", path, len(products))


def save_json(products: List[Product], path: str) -> None:
    """
    Write the list of products to a UTF-8 encoded JSON file.

    Output is a JSON array of objects, one object per product.
    indent=2 makes the file human-readable (pretty-printed).
    ensure_ascii=False allows Indonesian characters to be stored as-is
    rather than being escaped to \\uXXXX sequences.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            [asdict(p) for p in products],  # Convert all Products to dicts
            f,
            ensure_ascii = False,           # Keep Indonesian characters readable
            indent       = 2,               # Pretty-print with 2-space indentation
        )
    log.info("JSON -> %s  (%d records written)", path, len(products))


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------

def main() -> None:
    """
    Main function — orchestrates the full scraping run.

    Logs the configuration, runs the scraper, and saves both output files.
    If no products are collected (e.g. all requests blocked), logs a warning
    and exits cleanly without writing empty files.
    """
    log.info("=" * 60)
    log.info("Shopee Indonesia Product Scraper — starting")
    log.info("=" * 60)
    log.info("Keywords      : %s", KEYWORDS)
    log.info("Pages/keyword : %d", PAGES_PER_KEYWORD)
    log.info("Items/page    : %d", ITEMS_PER_PAGE)
    log.info("Max records   : ~%d", PAGES_PER_KEYWORD * ITEMS_PER_PAGE * len(KEYWORDS))
    log.info("=" * 60)

    # Run the full scrape across all keywords
    products = scrape_all(KEYWORDS)

    # Check if anything was actually collected before writing files
    if not products:
        log.warning("No products collected.")
        log.warning("Possible causes: IP rate-limited, cookies expired, network issue.")
        log.warning("See README.md for full anti-bot documentation.")
        return

    # Save outputs in both required formats
    save_csv(products, OUTPUT_CSV)
    save_json(products, OUTPUT_JSON)

    log.info("=" * 60)
    log.info("Done. %d unique products saved to %s and %s",
             len(products), OUTPUT_CSV, OUTPUT_JSON)
    log.info("=" * 60)


# This guard ensures main() only runs when the script is executed directly.
# If someone imports scraper.py as a module, main() will NOT run automatically.
if __name__ == "__main__":
    main()
