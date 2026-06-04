"""
CMC Historical Scraper
======================
Collects daily price & market-cap history for the top N cryptocurrencies
from CoinMarketCap and exports analysis-ready wide-format CSVs.

Usage:
    python crawl_cmc_historical.py

Install:
    pip install requests pandas beautifulsoup4 tqdm openpyxl
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ─────────────────────────────────────────────────────────────
#  CONFIG  ← edit here
# ─────────────────────────────────────────────────────────────
TARGET_COINS   = 600          # top N coins by market cap
START_DATE     = "2010-01-01" # earliest date to collect
OUTPUT_DIR     = Path("output")
CHECKPOINT_DIR = Path("checkpoint")
DELAY_PER_COIN = 3.0          # seconds between coins
DELAY_PER_PAGE = 2.0          # seconds between pagination calls
DELAY_429      = 90           # back-off on rate-limit (429)
MAX_RETRIES    = 5
# ─────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(exist_ok=True)
CHECKPOINT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("crawl_cmc_historical.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

VN_TZ = timezone(timedelta(hours=7))
TODAY = datetime.now(VN_TZ).strftime("%Y-%m-%d")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin":          "https://coinmarketcap.com",
    "Referer":         "https://coinmarketcap.com/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────

def get_with_retry(url, params=None, extra_headers=None):
    """GET with exponential back-off and retry logic."""
    headers = extra_headers or {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 200:
                return r
            elif r.status_code == 429:
                wait = DELAY_429 * attempt
                log.warning(f"  Rate limit — waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
            elif r.status_code == 403:
                log.warning(f"  403 Forbidden — waiting 30s (attempt {attempt})")
                time.sleep(30)
            elif r.status_code in (500, 502, 503, 504):
                log.warning(f"  Server error {r.status_code} — waiting 20s")
                time.sleep(20)
            else:
                log.error(f"  HTTP {r.status_code}: {url}")
                return None
        except Exception as exc:
            log.warning(f"  Request error: {exc} (attempt {attempt})")
            time.sleep(10 * attempt)
    return None


def parse_money(text):
    """Parse a '$1,234.56' style string to float."""
    if not text:
        return None
    text = text.strip().replace("$", "").replace(",", "").replace(" ", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(text):
    """Parse 'Jan 01, 2024' → '2024-01-01'."""
    try:
        return datetime.strptime(text.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
#  STEP 1 — Coin list (top N by market cap)
# ─────────────────────────────────────────────────────────────

def fetch_coin_list(total=600):
    """Return list of dicts with rank, name, symbol, slug, id."""
    checkpoint = CHECKPOINT_DIR / "coin_list.json"
    if checkpoint.exists():
        coins = json.loads(checkpoint.read_text())[:total]
        log.info(f"✓ Coin list loaded from checkpoint: {len(coins)} coins")
        return coins

    coins = []
    start = 1
    log.info(f"Fetching top {total} coins from CMC ...")

    while len(coins) < total:
        limit = min(200, total - len(coins))
        url = (
            "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
            f"?start={start}&limit={limit}"
            "&sortBy=market_cap&sortType=desc"
            "&convert=USD&cryptoType=all&tagType=all"
        )
        r = get_with_retry(url)
        if r is None:
            log.error("Failed to fetch coin list!")
            break

        items = r.json().get("data", {}).get("cryptoCurrencyList", [])
        if not items:
            break

        for item in items:
            coins.append({
                "rank":   item.get("cmcRank"),
                "name":   item.get("name"),
                "symbol": item.get("symbol", "").upper(),
                "slug":   item.get("slug"),
                "id":     item.get("id"),
            })

        log.info(f"  → {len(coins)}/{total}")
        start += len(items)
        if len(coins) < total:
            time.sleep(DELAY_PER_PAGE)

    coins = coins[:total]
    checkpoint.write_text(json.dumps(coins, indent=2, ensure_ascii=False))
    log.info(f"✓ Saved {len(coins)} coins to checkpoint")
    return coins


# ─────────────────────────────────────────────────────────────
#  STEP 2 — Historical data for one coin
# ─────────────────────────────────────────────────────────────

def fetch_history(slug, symbol, cmc_id):
    """
    Fetch full daily history for a single coin.
    Strategy 1: CMC internal JSON API (fast).
    Strategy 2: HTML table scraping (fallback).
    Results are checkpointed as CSV so interrupted runs resume cleanly.
    """
    checkpoint = CHECKPOINT_DIR / f"{slug}.csv"
    if checkpoint.exists():
        df = pd.read_csv(checkpoint)
        log.info(f"    ✓ Cache hit: {len(df)} rows")
        return df if not df.empty else None

    START_TS = 1262304000  # 2010-01-01 UTC
    END_TS   = int(datetime.now().timestamp())

    # ── Strategy 1: JSON API ──────────────────────────────────
    r = get_with_retry(
        "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/historical",
        params={
            "id":        cmc_id,
            "convertId": 2781,      # USD
            "timeStart": START_TS,
            "timeEnd":   END_TS,
            "interval":  "daily",
        },
        extra_headers={
            "Referer": f"https://coinmarketcap.com/currencies/{slug}/historical-data/",
        },
    )

    if r is not None:
        try:
            quotes = r.json().get("data", {}).get("quotes", [])
            if quotes:
                rows = []
                for q in quotes:
                    qt   = q.get("quote", {})
                    date = q.get("timeClose", "")[:10]
                    if date < START_DATE:
                        continue
                    rows.append({
                        "date":       date,
                        "close":      qt.get("close"),
                        "market_cap": qt.get("marketCap"),
                    })
                if rows:
                    df = (
                        pd.DataFrame(rows)
                        .sort_values("date")
                        .drop_duplicates("date")
                        .reset_index(drop=True)
                    )
                    df.to_csv(checkpoint, index=False)
                    log.info(
                        f"    API ✓  {len(df)} rows  "
                        f"({df['date'].iloc[0]} → {df['date'].iloc[-1]})"
                    )
                    return df
        except Exception as exc:
            log.warning(f"    API parse error: {exc} — falling back to HTML")

    # ── Strategy 2: HTML table (fallback) ────────────────────
    log.info(f"    → HTML fallback for {slug} ...")
    SESSION.headers.update({"Accept": "text/html,application/xhtml+xml,*/*"})
    r2 = get_with_retry(
        f"https://coinmarketcap.com/currencies/{slug}/historical-data/"
    )
    SESSION.headers.update({"Accept": "application/json, text/plain, */*"})

    if r2 is None:
        log.warning(f"    ✗ Both strategies failed: {slug}")
        pd.DataFrame(columns=["date", "close", "market_cap"]).to_csv(checkpoint, index=False)
        return None

    soup  = BeautifulSoup(r2.text, "html.parser")
    table = soup.find("table")
    if not table:
        log.warning(f"    ✗ No table found in HTML: {slug}")
        pd.DataFrame(columns=["date", "close", "market_cap"]).to_csv(checkpoint, index=False)
        return None

    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 7:
            continue
        date = parse_date(cells[0])
        if not date or date < START_DATE:
            continue
        rows.append({
            "date":       date,
            "close":      parse_money(cells[4]),   # Close column
            "market_cap": parse_money(cells[6]),   # Market Cap column
        })

    if not rows:
        log.warning(f"    ✗ Parsed 0 rows from HTML: {slug}")
        pd.DataFrame(columns=["date", "close", "market_cap"]).to_csv(checkpoint, index=False)
        return None

    df = (
        pd.DataFrame(rows)
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )
    df.to_csv(checkpoint, index=False)
    log.info(
        f"    HTML ✓  {len(df)} rows  "
        f"({df['date'].iloc[0]} → {df['date'].iloc[-1]})"
    )
    return df


# ─────────────────────────────────────────────────────────────
#  STEP 3 — Crawl all coins with checkpoint resume
# ─────────────────────────────────────────────────────────────

def crawl_all(coins):
    """Iterate over all coins, skipping any already checkpointed."""
    done_file = CHECKPOINT_DIR / "done.json"

    # Validate done.json against actual CSV files on disk
    done = set()
    if done_file.exists():
        for slug in json.loads(done_file.read_text()):
            if (CHECKPOINT_DIR / f"{slug}.csv").exists():
                done.add(slug)
        log.info(f"Resume: {len(done)}/{len(coins)} coins already collected")

    remaining = [c for c in coins if c["slug"] not in done]
    log.info(f"To crawl: {len(remaining)} coins")

    iterator = tqdm(remaining, desc="Crawling") if HAS_TQDM else remaining

    for i, coin in enumerate(iterator, 1):
        slug   = coin["slug"]
        symbol = coin["symbol"]
        cmc_id = coin["id"]

        if not HAS_TQDM:
            log.info(
                f"[{i:>3}/{len(remaining)}] "
                f"#{coin['rank']:>4}  {symbol:<12}  ({slug})"
            )

        try:
            fetch_history(slug, symbol, cmc_id)
        except Exception as exc:
            log.error(f"    CRASH [{slug}]: {exc}")

        done.add(slug)
        done_file.write_text(json.dumps(list(done)))
        time.sleep(DELAY_PER_COIN)

    log.info(f"\n✓ Crawl complete: {len(done)} coins")


# ─────────────────────────────────────────────────────────────
#  STEP 4 — Merge all checkpoints → wide CSVs
# ─────────────────────────────────────────────────────────────

def merge_and_export(coins):
    """
    Pivot all per-coin CSVs into two wide-format matrices:
      PRICE_<date>.csv  — daily close price per coin
      MCAP_<date>.csv   — daily market cap per coin
    Rows = dates, columns = coin symbols.
    """
    log.info("Merging all checkpoints into wide tables ...")

    all_dates = set()
    data_map  = {}  # slug → DataFrame

    for coin in coins:
        cp = CHECKPOINT_DIR / f"{coin['slug']}.csv"
        if not cp.exists():
            continue
        df = pd.read_csv(cp)
        if df.empty or "date" not in df.columns:
            continue
        df = df.dropna(subset=["date"])
        data_map[coin["slug"]] = df
        all_dates.update(df["date"].tolist())

    if not all_dates:
        log.error("No data found to merge!")
        return

    date_index = sorted(all_dates)
    log.info(
        f"Date range: {min(date_index)} → {max(date_index)}  "
        f"({len(date_index)} days | {len(data_map)} coins with data)"
    )

    price_wide = pd.DataFrame({"DATE": date_index})
    mcap_wide  = pd.DataFrame({"DATE": date_index})
    used_symbols = {}

    for coin in coins:
        slug   = coin["slug"]
        symbol = coin["symbol"]

        # Resolve duplicate symbols (e.g. two coins share ticker)
        if symbol in used_symbols:
            col = f"{symbol}_{slug}"
            log.warning(f"  Duplicate symbol {symbol} → using column name {col}")
        else:
            col = symbol
            used_symbols[symbol] = slug

        if slug not in data_map:
            price_wide[col] = None
            mcap_wide[col]  = None
            continue

        df = data_map[slug].rename(columns={"date": "DATE"})
        price_wide = price_wide.merge(
            df[["DATE", "close"]].rename(columns={"close": col}),
            on="DATE", how="left",
        )
        mcap_wide = mcap_wide.merge(
            df[["DATE", "market_cap"]].rename(columns={"market_cap": col}),
            on="DATE", how="left",
        )

    price_path = OUTPUT_DIR / f"PRICE_{TODAY}.csv"
    mcap_path  = OUTPUT_DIR / f"MCAP_{TODAY}.csv"

    price_wide.to_csv(price_path, index=False, encoding="utf-8-sig")
    mcap_wide.to_csv(mcap_path,  index=False, encoding="utf-8-sig")

    log.info(f"\n✅  PRICE → {price_path}  shape={price_wide.shape}")
    log.info(f"✅  MCAP  → {mcap_path}   shape={mcap_wide.shape}")
    log.info(f"    {price_wide.shape[0]} days × {price_wide.shape[1] - 1} coins")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"CMC HISTORICAL SCRAPER  |  top {TARGET_COINS}  |  {TODAY}")
    log.info("=" * 60)

    coins = fetch_coin_list(total=TARGET_COINS)
    log.info(f"Top 5: {[(c['symbol'], c['slug']) for c in coins[:5]]}")

    crawl_all(coins)
    merge_and_export(coins)

    log.info("\n" + "=" * 60)
    log.info("✅  DONE! Check the output/ folder.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
