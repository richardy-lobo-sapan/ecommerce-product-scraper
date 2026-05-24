# Shopee Indonesia Product Scraper

A Python scraper that collects product listings from [Shopee Indonesia](https://shopee.co.id) and exports them to `result.csv` and `result.json`.

Built as Section 3 of the DE Analytics Portfolio (Kredivo Technical Test).

---

## How It Works

Shopee's website is a Single Page Application that fetches product data from its own internal JSON search API. We call that same endpoint directly — no Selenium, no HTML parsing, clean structured JSON.

```
scraper.py
  └── build_session()         # curl_cffi session impersonating Chrome 124 TLS
  └── warm_up_session()       # visits homepage to acquire session cookies
  └── scrape_all(keywords)
       └── scrape_keyword()   # paginates 3 pages per keyword
            └── build_url()   # constructs API URL with offset params
            └── parse_item()  # extracts all fields from JSON response
  └── save_csv() / save_json()
```

### Why curl_cffi?

Major Indonesian e-commerce platforms use TLS fingerprinting — they read the SSL handshake itself to identify Python scripts before any headers are checked. `curl_cffi` wraps `libcurl` and copies Chrome 124's exact TLS cipher suites and extensions, making each connection indistinguishable from a real browser at the network level.

### Pagination

Shopee returns max 60 items per request. Pagination uses the `newest` offset:

| Page | `newest` | Items |
|------|----------|-------|
| 1 | 0 | 1–60 |
| 2 | 60 | 61–120 |
| 3 | 120 | 121–180 |

---

## Fields Collected

### Mandatory

| Field | Type | Description |
|-------|------|-------------|
| `product_name` | string | Product title as listed on Shopee |
| `category_id` | integer | Shopee's numeric category ID |
| `price_idr` | float | Price in IDR (raw value ÷ 100,000) |

### Extra fields (with business justification)

| Field | Type | Business value |
|-------|------|----------------|
| `rating` | float | Demand signal; input to price elasticity models |
| `review_count` | integer | Weights rating reliability |
| `units_sold` | integer | Sales velocity; GMV and inventory forecasting |
| `stock` | integer | Live inventory intelligence |
| `seller_location` | string | Geographic supply mapping for logistics analysis |
| `discount_pct` | integer | Promotional strategy; markdown optimisation input |
| `product_url` | string | Full traceability; enables price monitoring across runs |

---

## Requirements

- Python 3.9+
- pip

```bash
pip install -r requirements.txt
```

Only one external dependency: `curl_cffi` (handles both HTTP and TLS impersonation).

---

## How to Run

```bash
pip install -r requirements.txt
python scraper.py
```

Outputs `result.csv` and `result.json` in the working directory. The run takes approximately 10–15 minutes due to polite delays between requests.

### Customise

All settings are at the top of `scraper.py`:

```python
KEYWORDS               = ["laptop", "smartphone", ...]
PAGES_PER_KEYWORD      = 3       # × 60 items per page
DELAY_BETWEEN_REQUESTS = (1.5, 3.0)  # seconds between pages
```

---

## Output

`result.csv` and `result.json` in this repo contain sample data showing the full schema across 5 categories (laptop, smartphone, sepatu, skincare, baju pria).

### result.json structure

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

---

## Design Decisions

**curl_cffi over requests** — Standard `requests` is fingerprinted at the TLS layer by all major Indonesian e-commerce platforms. `curl_cffi` impersonates Chrome 124's TLS signature, making connections indistinguishable from a real browser.

**Fresh session per keyword** — Shopee rate-limits sessions after a few requests. Creating a new session (new TLS handshake + new cookies from homepage warm-up) for each keyword resets that counter.

**Single file** — The test spec asks for `scraper.py`. Everything is modular internally (single-responsibility functions, typed dataclass model) without needing multiple modules.

**@dataclass for Product** — Type-annotated, serialisable via `asdict()`, no boilerplate. Both `save_csv()` and `save_json()` use the same `asdict()` call.

**Discount normalisation** — Shopee returns discount as either an integer (`5`) or a formatted string (`"-5%"`). The parser strips `%` and `-` before converting to ensure consistent integer output.

---

## Ethical Compliance

| Concern | Approach |
|---------|----------|
| robots.txt | `/api/` search path is not disallowed on shopee.co.id |
| Rate limiting | 1.5–3s delay between pages; 8–15s pause between keywords |
| Credentials | None used — search endpoint is fully public |
| Personal data | No user profiles or private data collected |
