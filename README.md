# CMC Historical Scraper

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)
![Data](https://img.shields.io/badge/Data-CoinMarketCap-orange)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

> **End-to-end pipeline** that collects daily **price & market-cap history** for the top 600 cryptocurrencies from CoinMarketCap and outputs analysis-ready wide-format CSVs — no paid API key required.

---

## Problem Statement

Crypto research typically requires historical OHLCV data across hundreds of assets simultaneously. Public APIs either throttle aggressively or charge for bulk access. This scraper solves that by:

- Hitting CMC's internal JSON API (primary path, ~10× faster than HTML scraping)
- Falling back to HTML table parsing when the API returns no data
- Resuming interrupted runs via per-coin CSV checkpoints
- Exporting two **wide-format** matrices ready for pandas, Excel, or any BI tool

---

## Tech Stack

| Layer | Tools |
|---|---|
| HTTP / Session management | `requests` (retry logic, rate-limit handling) |
| HTML parsing | `BeautifulSoup4` |
| Data wrangling | `pandas` |
| Progress tracking | `tqdm` |
| Output format | CSV (`utf-8-sig` for Excel compatibility) |

---

## Output Schema

Two wide-format CSVs are produced in `output/`:

### `PRICE_YYYY-MM-DD.csv`
```
DATE        | BTC      | ETH      | BNB      | ...
2013-04-28  | 135.30   | NaN      | NaN      | ...
2015-08-07  | 278.42   | 1.20     | NaN      | ...
2024-01-01  | 42283.12 | 2256.34  | 301.21   | ...
```

### `MCAP_YYYY-MM-DD.csv`
Same structure, values = USD market capitalisation.

---

## Project Structure

```
cmc-historical-scraper/
├── crawl_cmc_historical.py   # Main pipeline script
├── requirements.txt          # Pinned dependencies
├── .gitignore                # Excludes output/, checkpoint/, logs
├── sample_output/            # Demo CSVs (10-coin, 30-day slice)
│   ├── PRICE_sample.csv
│   └── MCAP_sample.csv
└── README.md
```

Runtime artefacts (not tracked by git):
```
checkpoint/    # Per-coin CSVs + done.json (resume state)
output/        # Final wide-format exports
*.log          # Crawl logs
```

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/<your-username>/cmc-historical-scraper.git
cd cmc-historical-scraper

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Edit config at top of script
#    TARGET_COINS = 600   ← reduce for a quick test
#    START_DATE   = "2020-01-01"

# 4. Run
python crawl_cmc_historical.py
```

Output files appear in `output/` when complete.

---

## Configuration

All tuneable parameters live at the top of `crawl_cmc_historical.py`:

| Variable | Default | Description |
|---|---|---|
| `TARGET_COINS` | `600` | Number of coins to collect (ranked by market cap) |
| `START_DATE` | `"2010-01-01"` | Earliest date to include |
| `DELAY_PER_COIN` | `3.0s` | Polite delay between coins |
| `DELAY_429` | `90s` | Back-off on rate-limit response |
| `MAX_RETRIES` | `5` | Retry attempts per request |

---

## Pipeline Architecture

```
fetch_coin_list()          ← Top-N coins by market cap (paginated, cached)
        │
        ▼
crawl_all()
  └── fetch_history()      ← Per coin: JSON API → HTML fallback → CSV checkpoint
        │
        ▼
merge_and_export()         ← Pivot all checkpoints → wide PRICE + MCAP CSVs
```

**Fault tolerance:** each coin's data is checkpointed as an individual CSV. If the process is interrupted, re-running resumes from where it left off — only uncrawled coins are fetched.

---

## Sample Output

See [`sample_output/`](./sample_output/) for a 10-coin, 30-day demo slice.

---

## Notes & Limitations

- This scraper targets CoinMarketCap's internal (undocumented) API. CMC may change response schemas without notice.
- Data before 2013 is sparse; most coins only have data from their listing date onward.
- For commercial or high-frequency use cases, consider the [official CMC API](https://coinmarketcap.com/api/).

---

## Author

**Edward Nguyen** · [LinkedIn](https://linkedin.com/in/<your-linkedin>) · [GitHub](https://github.com/<your-username>)

*Built as part of a Data Analytics portfolio project — feedback and PRs welcome.*
