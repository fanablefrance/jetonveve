"""
VeVe catalogue scraper — via the my-nft-tracker public REST API.

Why this source
---------------
VeVe's own site is behind Cloudflare (returns 403 to bots) and its GraphQL API only
accepts pre-registered "persisted queries", so it can't be scraped directly.

my-nft-tracker.com is a community VeVe tracker that already aggregates the *entire*
VeVe catalogue and exposes it through a clean, unauthenticated REST API:

    https://my-nft-tracker-backend.azurewebsites.net/api/Nfts

That endpoint returns fully structured JSON (name, edition, type/category, rarity,
release date, mint amounts, store price, series, brand, licensor, images, and the
VeVe product UUID in `externalReference`). We simply paginate it — no browser, no
proxy, no anti-bot fight. At the time of writing it holds ~18,700 products.

This module fetches every product, flattens each into a tidy row, adds a direct VeVe
product URL and image URL, and returns the list. `sheets.py` handles the upsert.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any, Dict, List, Optional

import requests

API_BASE = "https://my-nft-tracker-backend.azurewebsites.net"
NFTS_URL = f"{API_BASE}/api/Nfts"

# IMPORTANT: the API misbehaves for large `limit` values (above ~24 it silently
# corrupts the query and returns overlapping windows, causing an infinite dup loop).
# 24 is the value the official site uses and is proven to paginate correctly.
PAGE_SIZE = 24
REQUEST_TIMEOUT = 60
MAX_RETRIES = 4
RETRY_BACKOFF = 3  # seconds, multiplied by attempt number
PAUSE_BETWEEN_PAGES = 0.25  # be polite to the free community backend

USER_AGENT = "veve-catalogue-sync/1.0 (personal catalogue export)"

# VeVe front-end URL patterns, by category, using the VeVe UUID (externalReference).
VEVE_URL_BY_CATEGORY = {
    "collectible": "https://www.veve.me/collectibles/en/collectibles/{uuid}",
    "comic": "https://www.veve.me/collectibles/en/collection/comic/{uuid}",
    "artwork": "https://www.veve.me/collectibles/en/artworks/{uuid}",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _get(session: requests.Session, params: Dict[str, Any]) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(NFTS_URL, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network hiccup / 5xx / throttling
            last_err = e
            wait = RETRY_BACKOFF * attempt
            print(f"    request failed (attempt {attempt}/{MAX_RETRIES}): {e} — retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Gave up fetching {params}: {last_err}")


def build_veve_url(category: Optional[str], external_ref: Optional[str],
                   series_uuid: Optional[str] = None) -> str:
    """Direct link to the VeVe product page.

    Comics live at /collection/comic/<COMIC id>, and the comic id equals the tracker's
    series.externalReference (our series_uuid) — NOT the per-rarity externalReference.
    Collectibles/artworks use their own externalReference.
    """
    key = (category or "").strip().lower()
    uuid = series_uuid if key == "comic" else external_ref
    if not uuid:
        return ""
    tmpl = VEVE_URL_BY_CATEGORY.get(key) or VEVE_URL_BY_CATEGORY["collectible"]
    return tmpl.format(uuid=uuid)


def _image_url(image_link: Optional[str]) -> str:
    if not image_link:
        return ""
    if image_link.startswith("http"):
        return image_link
    return f"{API_BASE}{image_link}"


def _num(x: Any) -> Any:
    return x


def _flatten_product(p: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one API product object into a flat, human-friendly row."""
    series = p.get("series") or {}
    brand = p.get("brand") or {}
    licensor = p.get("licensor") or {}
    stats = p.get("priceStatistics") or {}
    latest = (stats.get("latest") or {}) if isinstance(stats, dict) else {}
    atl = (stats.get("allTimeLowest") or {}) if isinstance(stats, dict) else {}
    ath = (stats.get("allTimeHighest") or {}) if isinstance(stats, dict) else {}

    def change_pct(obj: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get("percentChange", obj.get("percentageChange"))
        return None

    external_ref = p.get("externalReference")
    category = p.get("category")

    row = {
        # --- identity ---
        "veve_uuid": external_ref,
        "name": p.get("name"),
        "category": category,
        "edition": p.get("edition"),
        "rarity": p.get("rarity"),
        "releaseDate": p.get("releaseDate"),
        # --- supply ---
        "releaseAmount": p.get("releaseAmount"),
        "availableAmount": p.get("availableAmount"),
        # --- pricing (current snapshot) ---
        "storePrice": p.get("storePrice"),
        "market_lowestOffer": latest.get("lowestOffer"),
        "market_totalListings": latest.get("totalListings"),
        "allTimeLow": atl.get("lowestOffer"),
        "allTimeHigh": ath.get("lowestOffer"),
        "change_1d_pct": change_pct(stats.get("oneDayChange")) if isinstance(stats, dict) else None,
        "change_7d_pct": change_pct(stats.get("sevenDayChange")) if isinstance(stats, dict) else None,
        "change_30d_pct": change_pct(stats.get("thirtyDayChange")) if isinstance(stats, dict) else None,
        "gemsPerMcp": stats.get("gemsPerMcp") if isinstance(stats, dict) else None,
        "noMarketListing": stats.get("noMarketListing") if isinstance(stats, dict) else None,
        # --- relationships ---
        "series_name": series.get("seriesName"),
        "series_uuid": series.get("externalReference") or series.get("uuid"),
        "brand_name": brand.get("name"),
        "brand_uuid": brand.get("uuid"),
        "licensor_name": licensor.get("name"),
        "licensor_uuid": licensor.get("uuid"),
        # --- links ---
        "veve_url": build_veve_url(category, external_ref,
                                   series.get("externalReference") or series.get("uuid")),
        # image_url is owned by the VeVe enrichment (CloudFront); not set here so daily
        # runs never wipe an already-enriched image.
        # --- ids for reference/joining ---
        "tracker_uuid": p.get("uuid"),
    }
    return row


def scrape_catalogue(category: Optional[str] = None, limit_total: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch the full VeVe catalogue.

    category: None = everything (collectibles + comics). Or "collectible" / "comic".
    limit_total: stop after this many products (useful for a quick test run).
    """
    session = _session()
    base_params = {
        "orderBy": "releaseDate",
        "orderAsc": "false",
        "showOnlyReleased": "",
    }
    if category:
        base_params["category"] = category

    first = _get(session, {**base_params, "offset": 0, "limit": PAGE_SIZE})
    meta = first.get("meta", {})
    total = int(meta.get("entries_TotalAvailable", 0))
    print(f"Catalogue size reported: {total} products (category={category or 'ALL'})", flush=True)

    by_uuid: Dict[str, Dict[str, Any]] = {}

    def ingest(entries: List[Dict[str, Any]]) -> None:
        for p in entries:
            row = _flatten_product(p)
            key = row.get("veve_uuid") or row.get("tracker_uuid")
            if key:
                by_uuid[key] = row

    ingest(first.get("resultEntries", []))

    offset = len(first.get("resultEntries", []))
    reqs = 1
    while offset < total:
        if limit_total and len(by_uuid) >= limit_total:
            break
        data = _get(session, {**base_params, "offset": offset, "limit": PAGE_SIZE})
        entries = data.get("resultEntries", [])
        if not entries:
            print("    empty page — stopping.", flush=True)
            break
        ingest(entries)
        offset += len(entries)
        reqs += 1
        if reqs % 25 == 0:
            print(f"    ... {len(by_uuid)}/{total} products", flush=True)
        time.sleep(PAUSE_BETWEEN_PAGES)

    products = list(by_uuid.values())
    print(f"TOTAL harvested: {len(products)} unique products", flush=True)
    return products


def _parse_release(x: Any) -> Optional[_dt.datetime]:
    if not x:
        return None
    sx = str(x).strip().replace("Z", "")
    try:
        return _dt.datetime.fromisoformat(sx)
    except Exception:
        try:
            return _dt.datetime.strptime(sx[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def scrape_window(days_back: int = 8) -> List[Dict[str, Any]]:
    """Light daily fetch: only UPCOMING drops + items released in the last `days_back`
    days. Ordered by releaseDate DESC, so future drops come first, then recent ones;
    we stop as soon as we pass the cutoff. Keeps requests to my-nft-tracker minimal.
    """
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days_back)
    session = _session()
    base_params = {"orderBy": "releaseDate", "orderAsc": "false", "showOnlyReleased": ""}

    first = _get(session, {**base_params, "offset": 0, "limit": PAGE_SIZE})
    total = int(first.get("meta", {}).get("entries_TotalAvailable", 0))
    by_uuid: Dict[str, Dict[str, Any]] = {}

    def ingest(entries) -> bool:
        reached_old = False
        for p in entries:
            row = _flatten_product(p)
            key = row.get("veve_uuid") or row.get("tracker_uuid")
            if key:
                by_uuid[key] = row
            rd = _parse_release(row.get("releaseDate"))
            if rd is not None and rd < cutoff:
                reached_old = True
        return reached_old

    stop = ingest(first.get("resultEntries", []))
    offset = len(first.get("resultEntries", []))
    while not stop and offset < total:
        data = _get(session, {**base_params, "offset": offset, "limit": PAGE_SIZE})
        entries = data.get("resultEntries", [])
        if not entries:
            break
        stop = ingest(entries)
        offset += len(entries)
        time.sleep(PAUSE_BETWEEN_PAGES)

    products = list(by_uuid.values())
    print(f"Window (upcoming + last {days_back}d): {len(products)} products, "
          f"{offset} scanned.", flush=True)
    return products


if __name__ == "__main__":
    import sys
    lim = 300 if "--test" in sys.argv else None
    items = scrape_catalogue(limit_total=lim)
    print(f"Got {len(items)} products.")
    if items:
        print("Columns:", ", ".join(items[0].keys()))
        print("Sample:", items[0])
