# Shopee Indonesia Product Scraper

A Python scraper that collects product listings from [Shopee Indonesia](https://shopee.co.id) and exports them to `result.csv` and `result.json`.

Built as part of the DE Analytics Portfolio (Kredivo Technical Test).

---

## Table of Contents

- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Fields Collected](#fields-collected)
- [Requirements](#requirements)
- [How to Run](#how-to-run)
- [Expected Output](#expected-output)
- [Design Decisions](#design-decisions)
- [Ethical Compliance](#ethical-compliance)

---

## How It Works

Shopee's website is a Single Page Application (SPA). When you search for a product in your browser, it doesn't load a new HTML page — it calls Shopee's own internal JSON API in the background and renders the results. We call that same API endpoint directly.

```
Browser / scraper
      │
      │  GET shopee.co.id/api/v4/search/search_items/?keyword=laptop&limit=60&newest=0
      │
      ▼
Shopee Search API
      │
      │  { "items": [ { "item_basic": { "name": "...", "price": ..., ... } }, ... ] }
      │
      ▼
scraper.py
  parse_item()  →  Product dataclass
  save_csv()    →  result.csv
  save_json()   →  result.json
```

This means:
- **No Selenium or browser automation needed** — plain `requests` is enough
- **No HTML parsing** — we get clean, structured JSON straight from Shopee
- The code is fast, readable, and has a minimal dependency footprint (2 packages)

### Pagination

Shopee returns a maximum of 60 items per request. To collect more data, we paginate by incrementing the `newest` offset parameter:

| Page | `newest` parameter | Items fetched |
|------|-------------------|---------------|
| 1    | 0                 | items 1–60    |
| 2    | 60                | items 61–120  |
| 3    | 120               | items 121–180 |

With 5 keywords × 3 pages × 60 items, the scraper collects up to ~900 unique products per run.

### Call flow

```
main()
 └── scrape_all(keywords)
      ├── build_session()          # one persistent HTTP session with retry adapter
      └── for each keyword:
           └── scrape_keyword(session, keyword)
                ├── build_url(keyword, offset)     # construct API URL with params
                ├── build_headers(keyword)          # browser-like headers, rotated UA
                ├── session.get(url, headers)       # make the HTTP request
                ├── parse_item(raw_dict)            # extract fields, return Product
                └── time.sleep(random delay)        # polite pause between requests
 └── save_csv(products, "result.csv")
 └── save_json(products, "result.json")
```

---

## Project Structure

```
scraper/
├── scraper.py        # All scraping logic — single self-contained file
├── requirements.txt  # Two dependencies: requests and urllib3
├── result.csv        # Output — one row per product
└── result.json       # Output — array of product objects
```

---

## Fields Collected

### Mandatory fields (required by test spec)

| Field          | Type    | Description                                                        |
|----------------|---------|--------------------------------------------------------------------|
| `product_name` | string  | Product title exactly as listed on Shopee                          |
| `category_id`  | integer | Shopee's numeric category ID (`catid`) from the API response       |
| `price_idr`    | float   | Listed price in Indonesian Rupiah (converted from Shopee's raw int)|

> **Note on `category_id`:** Shopee's search API returns a numeric category ID rather than a human-readable label. The name-to-ID mapping is available via a separate category API endpoint and can be joined at analysis time. Using the ID keeps this scraper stateless and the data lossless.

> **Note on `price_idr`:** Shopee stores prices internally as integers scaled by 100,000 (e.g. raw value `15000000` = IDR 150,000). `parse_price()` divides by 100,000 to recover the real price.

### Extra fields (with business justification)

| Field             | Type    | Why it's useful                                                                       |
|-------------------|---------|---------------------------------------------------------------------------------------|
| `rating`          | float   | Average star rating — demand quality signal; input to price elasticity models         |
| `review_count`    | integer | Total reviews — weights the reliability of `rating` (4.9★ from 3 reviews ≠ 4.9★ from 3,000) |
| `units_sold`      | integer | Cumulative units sold — sales velocity; critical for GMV modelling and inventory forecasting |
| `stock`           | integer | Current stock level — live inventory intelligence; triggers low-stock alerts          |
| `seller_location` | string  | Seller's city/region — geographic supply mapping; input to logistics cost analysis    |
| `discount_pct`    | integer | Active discount percentage — promotional strategy signal; input to markdown optimisation |
| `product_url`     | string  | Canonical listing URL — full traceability back to source; enables price monitoring across runs |

All extra fields are `Optional` — Shopee may omit any of them for some listings (e.g. new items have no reviews, some sellers hide stock levels). The scraper handles this gracefully without skipping the row.

---

## Requirements

- Python 3.10 or higher
- pip

Install dependencies:

```bash
pip install -r requirements.txt
```

**That's it.** The scraper intentionally uses only two external packages:

| Package    | Purpose                                          |
|------------|--------------------------------------------------|
| `requests` | HTTP session, connection pooling, retry adapter  |
| `urllib3`  | Provides the `Retry` class used for backoff logic|

Everything else (`csv`, `json`, `logging`, `dataclasses`, `time`, `random`) is Python standard library.

---

## How to Run

### 1. Clone and enter the project

```bash
git clone https://github.com/yourusername/de-analytics-portfolio.git
cd de-analytics-portfolio/scraper
```

### 2. (Recommended) Create a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the scraper

```bash
python scraper.py
```

### 5. Check the outputs

```
scraper/
├── result.csv    ← generated
└── result.json   ← generated
```

### What you'll see in the terminal

```
09:14:01  INFO  Shopee Indonesia Product Scraper — starting
09:14:01  INFO  Keywords : ['laptop', 'smartphone', 'sepatu', 'skincare', 'baju pria']
09:14:01  INFO  Scope    : 3 pages × 60 items × 5 keywords (max ~900 records)
09:14:01  INFO  ─── Keyword: 'laptop'
09:14:01  INFO    keyword='laptop'        page=1/3  offset=0
09:14:04  INFO    → 58/60 items accepted
09:14:06  INFO    keyword='laptop'        page=2/3  offset=60
09:14:09  INFO    → 60/60 items accepted
...
09:16:42  INFO  Total unique products collected: 847
09:16:42  INFO  CSV  → result.csv  (847 rows)
09:16:42  INFO  JSON → result.json  (847 records)
09:16:42  INFO  Done.
```

The run takes approximately 3–5 minutes, depending on network speed and the random delays between requests.

### Customising the scraper

All configuration lives at the top of `scraper.py` — no config files needed:

```python
KEYWORDS              = ["laptop", "smartphone", ...]  # what to search
PAGES_PER_KEYWORD     = 3     # increase for more data (each page = 60 items)
DELAY_BETWEEN_REQUESTS = (1.5, 3.0)  # min/max seconds between requests
```

---

## Expected Output

### result.csv

```
product_name,category_id,price_idr,rating,review_count,units_sold,stock,seller_location,discount_pct,product_url
ASUS VivoBook 15 Core i5 8GB 512GB SSD,11044,7299000.0,4.88,2341,876,45,Jakarta Pusat,15,https://shopee.co.id/product/123/456
Xiaomi Redmi Note 13 Pro 5G 8/256GB,11269,3399000.0,4.87,8934,12650,500,Surabaya,12,https://shopee.co.id/product/789/012
```

### result.json

```json
[
  {
    "product_name": "ASUS VivoBook 15 Core i5 8GB 512GB SSD",
    "category_id": 11044,
    "price_idr": 7299000.0,
    "rating": 4.88,
    "review_count": 2341,
    "units_sold": 876,
    "stock": 45,
    "seller_location": "Jakarta Pusat",
    "discount_pct": 15,
    "product_url": "https://shopee.co.id/product/123/456"
  }
]
```

Fields that Shopee does not return for a listing will appear as `null` in JSON and as an empty cell in CSV.

---

## Design Decisions

### Why Shopee's internal JSON API instead of HTML scraping?

Shopee is a SPA — the HTML source contains almost no product data. Scraping the rendered DOM would require Selenium or Playwright (adding a browser binary dependency, high memory use, and significant complexity). The internal JSON API returns clean, structured data with one straightforward `requests.get()` call, producing more readable and maintainable code.

### Why a single file (`scraper.py`) instead of multiple modules?

The test spec asks for `scraper.py` as the deliverable. Keeping everything in one self-contained file makes it trivially easy to read, review, and run — no import path confusion, no package setup. The code is still fully modular internally (functions with single responsibilities, a typed data model, separated concerns).

### Why `@dataclass` for the Product model?

`@dataclass` gives us: type annotations for every field (readable, self-documenting), free `__repr__` for debugging, and trivial serialisation via `asdict()` which both `save_csv()` and `save_json()` use. It's also the modern Python way to define plain data containers — no boilerplate `__init__`.

### Why retry with exponential backoff instead of a simple loop?

A naive retry loop (`for i in range(3): try...`) retries immediately — if Shopee returned a 429 because of rate limiting, hitting it again instantly will just get another 429. Exponential backoff (`urllib3.Retry` with `backoff_factor=2`) waits 2 s, then 4 s, then 8 s — giving the server time to recover and giving us a much higher chance of eventual success without hammering the endpoint.

### Why rotate user agents?

A single hardcoded User-Agent is a trivial fingerprint that anti-bot systems use to identify scrapers. Rotating across 5 realistic browser profiles (Chrome/Firefox/Edge on Windows/macOS) makes individual requests indistinguishable from normal browser traffic.

### Why deduplication across keywords?

The same product (e.g. a popular laptop) can appear in results for both "laptop" and "asus laptop". Without deduplication the dataset would have duplicate rows, skewing any downstream analysis (counts, averages, price distributions). We deduplicate by `product_url` (unique per listing) with `product_name` as a fallback.

---

## Ethical Compliance

| Concern                        | How we handle it                                                            |
|-------------------------------|-----------------------------------------------------------------------------|
| `robots.txt`                  | The `/api/` search path is not disallowed on `shopee.co.id`                 |
| Rate limiting                 | Random 1.5–3.0 s delay between every request; exponential backoff on 429   |
| Authentication / credentials  | None used — the search endpoint is fully public                             |
| Personal data                 | No user profiles, purchase history, or private data is collected            |
| Server load                   | Max ~1 request every 1.5 s; retry backoff reduces load on struggling servers|
