"""Fetch raw Tesco listing and product JSON into data/raw/ (no parsing; see build.py).

1. Page through each category listing, skipping sponsored items, and write
   data/raw/_listing.json (listing fields plus the categories of every product).
2. Fetch product(tpnc) for every listed product into data/raw/{tpnc}.json.
   Cached files are skipped unless --refresh, so an interrupted run can just be re-run.
   Failures from the latest run are written to data/raw/_failures.json.

Usage:
    python scraper/fetch.py --category "Fresh Food" --limit 200   # test run
    python scraper/fetch.py                                       # all five categories
    python scraper/fetch.py --skip-listing                        # reuse _listing.json, fetch what's missing
"""

import argparse
import base64
import datetime
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
LISTING_PATH = RAW_DIR / "_listing.json"
FAILURES_PATH = RAW_DIR / "_failures.json"

load_dotenv(ROOT / ".env")

API_URL = "https://xapi.tesco.com/"

CATEGORIES = ["Fresh Food", "Bakery", "Frozen Food", "Food Cupboard", "Treats & Snacks"]

# Fields shared by the listing and product queries (all verified 2026-09-28).
PRODUCT_FIELDS = """
      tpnc tpnb gtin title brandName departmentName aisleName shelfName
      defaultImageUrl averageWeight isForSale status
      price { actual unitPrice unitOfMeasure }
      promotions { id startDate endDate description unitSellingInfo attributes }
"""

LISTING_QUERY = """
query Listing($facet: ID, $offset: Int, $count: Int) {
  category(facet: $facet, offset: $offset, count: $count) {
    info { total }
    products {
      adId
%s
    }
  }
}
""" % PRODUCT_FIELDS

PRODUCT_QUERY = """
query Product($tpnc: String) {
  product(tpnc: $tpnc) {
%s
    details {
      packSize { value units }
      nutrition { name value1 value2 value3 value4 }
    }
  }
}
""" % PRODUCT_FIELDS

# Stop the run if this many products fail in a row (likely blocked or the API is down).
MAX_CONSECUTIVE_FAILURES = 15


def log(message: str) -> None:
    print(message, flush=True)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def build_category_facet(label: str) -> str:
    # Mimic JS encodeURI, which Tesco uses: "Treats & Snacks" -> "Treats%20&%20Snacks"
    encoded = base64.b64encode(urllib.parse.quote(label, safe=";,/?:@&=+$!*'()#").encode()).decode()
    return f"b;{encoded}"


def read_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj) -> None:
    # Write to a temp file and rename, so an interrupted run never leaves a truncated cache file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class ApiError(Exception):
    """A request that failed for good: not retryable, or out of retries."""


class Client:
    """Thread-safe GraphQL client with a shared rate limit, retries and exponential backoff.

    Each thread gets its own requests.Session. All workers share one request schedule, so
    together they stay under `rate` requests/second. A retryable error (429, 5xx, network)
    pushes that schedule back, so every worker pauses, not just the one that hit it.
    """

    def __init__(self, api_key: str, rate: float, retries: int = 4):
        self.headers = {
            "x-apikey": api_key,
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "region": "UK",
            "language": "en-GB",
        }
        self.interval = 1.0 / rate
        self.retries = retries
        self._local = threading.local()
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.throttled = 0  # 429 responses so far (reported in progress lines)

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            self._local.session.headers.update(self.headers)
        return self._local.session

    def _wait_turn(self) -> None:
        with self._lock:
            start = max(time.monotonic(), self._next_at)
            self._next_at = start + self.interval
        time.sleep(max(0.0, start - time.monotonic()))

    def _cool_down(self, seconds: float) -> None:
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)

    def post(self, query: str, variables: dict) -> dict:
        error = None
        for attempt in range(self.retries + 1):
            if attempt:
                backoff = 2 ** (attempt - 1) + random.uniform(0, 1)
                # A single 429 is routine (counted in the progress line); log anything more persistent.
                if attempt > 1 or error != "HTTP 429":
                    log(f"    retry {attempt}/{self.retries} in {backoff:.1f}s ({error}) {variables}")
                self._cool_down(backoff)
            self._wait_turn()
            try:
                response = self._session().post(
                    API_URL, json={"query": query, "variables": variables}, timeout=30
                )
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    error = f"invalid JSON: {response.text[:200]!r}"
            elif response.status_code == 429 or response.status_code >= 500:
                # 429s carry Retry-After: 1 and X-RateLimit-Limit: 100, even at 2 req/s, so the
                # limit is probably shared by everyone using the public web key.
                error = f"HTTP {response.status_code}"
                if response.status_code == 429:
                    with self._lock:
                        self.throttled += 1
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    self._cool_down(int(retry_after))
            else:
                raise ApiError(f"HTTP {response.status_code}: {response.text[:200]!r}")
        raise ApiError(f"gave up after {self.retries + 1} attempts: {error}")


def fetch_listing(client: Client, category: str, page_size: int, limit: int | None) -> tuple[int, list[dict]]:
    """Page through one category's listing. Returns (Tesco's total, organic products in listing order)."""
    facet = build_category_facet(category)
    products, seen = [], set()
    offset, total, ads, duplicates = 0, None, 0, 0

    while total is None or offset < total:
        count = page_size if limit is None else min(page_size, limit - len(products))
        if count <= 0:
            break
        payload = client.post(LISTING_QUERY, {"facet": facet, "offset": offset, "count": count})
        if payload.get("errors"):
            raise ApiError(f"GraphQL errors: {json.dumps(payload['errors'])[:300]}")
        data = (payload.get("data") or {}).get("category") or {}
        total = (data.get("info") or {}).get("total") or 0
        page = data.get("products") or []

        # Sponsored items come on top of `count` organic ones; advance by the organic count only.
        organic = [p for p in page if not p.get("adId")]
        ads += len(page) - len(organic)
        if not organic:
            break
        for product in organic:
            product.pop("adId", None)
            if product["tpnc"] in seen:
                duplicates += 1
            else:
                seen.add(product["tpnc"])
                products.append(product)
        offset += len(organic)
        log(f"  {category}: {offset}/{total}")

    log(f"  {category}: {len(products)} unique products ({total} listed by Tesco, "
        f"{ads} ads skipped, {duplicates} duplicates)")
    if not total:
        log(f"  WARNING: {category}: Tesco lists no products. Is the label spelled exactly as on the site?")
    elif limit is None and len(products) != total:
        log(f"  WARNING: {category}: got {len(products)} unique products but Tesco reports {total}")
    return total, products


def save_listing(results: dict[str, tuple[int, list[dict]]], limit: int | None) -> dict:
    """Merge freshly listed categories into _listing.json, keeping other categories from earlier runs."""
    listing = read_json(LISTING_PATH) if LISTING_PATH.exists() else {"categories": {}, "products": {}}
    fetched = now_iso()
    for category, (total, products) in results.items():
        listing["categories"][category] = {
            "fetched": fetched,
            "total": total,
            "limit": limit,
            "tpncs": [p["tpnc"] for p in products],
        }
        for product in products:
            listing["products"][product["tpnc"]] = product

    # Categories overlap, so recompute every product's category list across all listed categories.
    membership: dict[str, list[str]] = {}
    for category, info in listing["categories"].items():
        for tpnc in info["tpncs"]:
            membership.setdefault(tpnc, []).append(category)
    listing["products"] = {
        tpnc: {**product, "categories": membership[tpnc]}
        for tpnc, product in listing["products"].items()
        if tpnc in membership
    }
    listing["updated"] = fetched
    write_json(LISTING_PATH, listing)
    log(f"Wrote {LISTING_PATH.relative_to(ROOT)}: {len(listing['products'])} unique products "
        f"across {len(listing['categories'])} categories")
    return listing


def fetch_product(client: Client, tpnc: str) -> None:
    payload = client.post(PRODUCT_QUERY, {"tpnc": tpnc})
    product = (payload.get("data") or {}).get("product")
    if not product:
        raise ApiError(f"no product in response: {json.dumps(payload.get('errors'))[:300]}")
    record = {"tpnc": tpnc, "fetched": now_iso(), "product": product}
    if payload.get("errors"):
        record["errors"] = payload["errors"]
    write_json(RAW_DIR / f"{tpnc}.json", record)


def fetch_products(client: Client, tpncs: list[str], workers: int, refresh: bool) -> dict[str, str]:
    """Fetch every product that isn't cached yet. Returns {tpnc: error} for failures."""
    todo = [t for t in tpncs if refresh or not (RAW_DIR / f"{t}.json").exists()]
    log(f"Products: {len(tpncs)} listed, {len(tpncs) - len(todo)} cached, {len(todo)} to fetch "
        f"with {workers} workers")
    failures: dict[str, str] = {}
    done, consecutive_failures, start = 0, 0, time.monotonic()

    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(fetch_product, client, tpnc): tpnc for tpnc in todo}
    try:
        for future in as_completed(futures):
            tpnc = futures[future]
            done += 1
            try:
                future.result()
                consecutive_failures = 0
            except Exception as exc:
                failures[tpnc] = f"{type(exc).__name__}: {exc}"
                consecutive_failures += 1
                log(f"  FAILED {tpnc}: {failures[tpnc]}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"Stopping: {consecutive_failures} failures in a row. Check the errors above, "
                        f"then re-run to resume.")
                    break
            if done % 50 == 0 or done == len(todo):
                elapsed = time.monotonic() - start
                rate = done / elapsed if elapsed else 0.0
                eta = datetime.timedelta(seconds=round((len(todo) - done) / rate)) if rate else "?"
                log(f"  {done}/{len(todo)} done, {len(failures)} failed, {client.throttled} throttled (429), "
                    f"{rate:.1f}/s, ETA {eta}")
    except KeyboardInterrupt:
        log("Interrupted; waiting for in-flight requests to finish...")
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        write_json(FAILURES_PATH, {"updated": now_iso(), "failures": failures})
        if failures:
            log(f"{len(failures)} failures logged to {FAILURES_PATH.relative_to(ROOT)}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch raw Tesco listing and product JSON into data/raw/.")
    parser.add_argument("--category", action="append", metavar="LABEL",
                        help="Category to fetch; repeatable. Default: all five top-level food categories.")
    parser.add_argument("--limit", type=int, help="Only the first N products of each category (for test runs).")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch products that are already cached.")
    parser.add_argument("--skip-listing", action="store_true",
                        help="Reuse data/raw/_listing.json instead of paging the category listings again.")
    parser.add_argument("--workers", type=int, default=3, choices=[1, 2, 3, 4], help="Concurrent requests (default 3).")
    parser.add_argument("--page-size", type=int, default=50, help="Organic products per listing page (default 50).")
    parser.add_argument("--rate", type=float, default=2.0,
                        help="Maximum requests per second across all workers (default 2).")
    args = parser.parse_args()

    api_key = os.environ.get("TESCO_API_KEY")
    if not api_key:
        sys.exit("TESCO_API_KEY is not set. Copy .env.example to .env and add the key.")
    categories = args.category or CATEGORIES
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client = Client(api_key, rate=args.rate)

    if args.skip_listing:
        if not LISTING_PATH.exists():
            sys.exit(f"{LISTING_PATH.relative_to(ROOT)} does not exist; run without --skip-listing first.")
        listing = read_json(LISTING_PATH)
        missing = [c for c in categories if c not in listing["categories"]]
        if missing:
            sys.exit(f"Not in the saved listing: {', '.join(missing)}")
        per_category = {c: listing["categories"][c]["tpncs"][: args.limit] for c in categories}
    else:
        results = {}
        for category in categories:
            log(f"Listing {category}...")
            results[category] = fetch_listing(client, category, args.page_size, args.limit)
        save_listing(results, args.limit)
        per_category = {c: [p["tpnc"] for p in products] for c, (_, products) in results.items()}

    tpncs = list(dict.fromkeys(t for c in categories for t in per_category[c]))  # dedupe, keep order
    try:
        fetch_products(client, tpncs, args.workers, args.refresh)
    except KeyboardInterrupt:
        sys.exit(130)

    missing = sum(not (RAW_DIR / f"{t}.json").exists() for t in tpncs)
    if missing:
        log(f"{missing} listed products have no cached file yet. Re-run with --skip-listing to retry them.")
    else:
        log(f"All {len(tpncs)} listed products are cached in {RAW_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
