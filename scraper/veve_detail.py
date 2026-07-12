"""
VeVe collectible enrichment via VeVe's own GraphQL API.

Discovery (validated live):
- VeVe's GraphQL endpoint https://web.api.prod.veve.me/graphql is directly callable
  from a plain server (datacenter IP) — NO browser, NO cookies — as long as the custom
  client headers below are present (they replace VeVe's header-based CSRF check).
- The site (www.veve.me) is Cloudflare-protected, but the *API host* is not.
- `publicCollectibleType(id)` returns the full detail for a COLLECTIBLE. The `id` equals
  the my-nft-tracker `externalReference` (our `veve_uuid`) for collectibles.
- COMICS are a different type (`publicComicType`) AND their VeVe ids don't match the
  tracker ids, so comics are NOT enriched here (handled separately, later).

Egress:
- If APIFY_PROXY_PASSWORD is set, requests are routed through Apify's RESIDENTIAL proxy
  (robust against any future datacenter-IP blocking). Otherwise they go out directly
  (also works today). Controlled entirely by env vars — no code change needed to switch.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

GRAPHQL_URL = "https://web.api.prod.veve.me/graphql"

# Headers that satisfy VeVe's header-based CSRF/anti-forgery check.
HEADERS = {
    "content-type": "application/json",
    "x-auth-version": "2",
    "client-name": "veve-app-web-server",
    "client-version": "1.0",
    "client-operation": "publicStoreCollectibleEditionsQuery",
    "user-agent": "Mozilla/5.0 (compatible; veve-catalogue-sync/1.0)",
    "accept": "application/json",
}

# Validated field set on publicCollectibleType (see module docstring).
QUERY = (
    "query publicStoreCollectibleEditionsQuery($id: ID!){ "
    "publicCollectibleType(id:$id){ "
    "__typename id name description rarity editionType isSpecialEdition "
    "storePrice marketFee dailyMcpPoints dropMethod dropDate "
    "totalIssued totalStoreAllocation totalAvailable soldEditions "
    "editionsBurnt withheldEditions editionsInCirculation availableReservations "
    "firstAvailableEdition isTotalAvailableVisible "
    "series{ id name season isBlindbox } "
    "brand{ id name licensor{ name } } "
    "image{ url webpUrl } } }"
)


# COMICS are a different VeVe type. The VeVe comic id equals the my-nft-tracker
# `series.externalReference` (our `series_uuid`). Each comic groups all its rarities,
# so the edition totals below are comic-level (sum across rarities). We apply the
# comic-level fields to every tracker rarity-row that shares the same comic id.
COMIC_QUERY = (
    "query publicStoreCollectibleEditionsQuery($id: ID!){ "
    "publicComicType(id:$id){ "
    "id name description storePrice marketFee dailyMcpPoints dropMethod dropDate "
    "startYear totalIssued totalAvailable soldEditions editionsBurnt "
    "editionsInCirculation withheldEditions firstAvailableEdition "
    "media{ edges{ node{ url } } } } }"
)

# Slim queries used for the DYNAMIC refresh of variable fields (editions counts,
# store price, allocation). Collectibles are refreshed several times a day.
DYN_COLLECTIBLE_QUERY = (
    "query publicStoreCollectibleEditionsQuery($id: ID!){ "
    "publicCollectibleType(id:$id){ id storePrice soldEditions totalAvailable "
    "totalStoreAllocation editionsBurnt editionsInCirculation withheldEditions } }"
)
DYN_COMIC_QUERY = (
    "query publicStoreCollectibleEditionsQuery($id: ID!){ "
    "publicComicType(id:$id){ id storePrice soldEditions totalAvailable "
    "editionsBurnt editionsInCirculation withheldEditions } }"
)

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 2
DEFAULT_WORKERS = 6
PAUSE = 0.05  # small politeness delay per request

_thread_local = threading.local()
_egress_lock = threading.Lock()
_egress_checked = False
_use_proxy = False  # decided at runtime by _decide_egress()


def _proxies() -> Optional[Dict[str, str]]:
    pwd = os.environ.get("APIFY_PROXY_PASSWORD")
    if not pwd:
        return None
    # Apify Proxy as a standard HTTP proxy, residential group.
    url = f"http://groups-RESIDENTIAL:{pwd}@proxy.apify.com:8000"
    return {"http": url, "https": url}


def _decide_egress() -> None:
    """Decide once whether to use the Apify proxy. If it is configured but fails
    (e.g. the plan has no residential proxy -> 403 tunnel), fall back to a DIRECT
    connection, which works fine. This keeps the run from failing wholesale."""
    global _egress_checked, _use_proxy
    with _egress_lock:
        if _egress_checked:
            return
        _egress_checked = True
        px = _proxies()
        if not px:
            _use_proxy = False
            print("Egress: direct connection (no APIFY_PROXY_PASSWORD set).", flush=True)
            return
        probe = {
            "operationName": "publicStoreCollectibleEditionsQuery",
            "variables": {"id": "8648d886-ed81-4ea1-bae7-cb1a0bc975bd"},
            "query": QUERY,
        }
        try:
            r = requests.post(GRAPHQL_URL, headers=HEADERS, json=probe,
                              proxies=px, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200 and "data" in r.text:
                _use_proxy = True
                print("Egress: Apify residential proxy OK.", flush=True)
                return
            print(f"Egress: proxy returned HTTP {r.status_code}; falling back to DIRECT.", flush=True)
        except Exception as e:
            print(f"Egress: Apify proxy unavailable ({e.__class__.__name__}); "
                  f"falling back to DIRECT connection.", flush=True)
        _use_proxy = False


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        if _use_proxy:
            px = _proxies()
            if px:
                s.proxies.update(px)
        _thread_local.session = s
    return s


def _maybe_disable_proxy(exc: Exception) -> None:
    """If a proxy/tunnel error occurs mid-run, drop the proxy and go direct."""
    global _use_proxy
    name = exc.__class__.__name__
    if _use_proxy and ("Proxy" in name or "Tunnel" in str(exc) or "proxy" in str(exc).lower()):
        _use_proxy = False
        _thread_local.__dict__.pop("session", None)
        print("Egress: proxy failed mid-run — switching to DIRECT for the rest.", flush=True)


def _num(x: Any) -> Any:
    if x in (None, ""):
        return x
    try:
        f = float(x)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return x


def fetch_collectible(uuid: str) -> Optional[Dict[str, Any]]:
    """Return enrichment columns for one collectible uuid, or None if not found."""
    payload = {
        "operationName": "publicStoreCollectibleEditionsQuery",
        "variables": {"id": uuid},
        "query": QUERY,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().post(GRAPHQL_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                node = (data.get("data") or {}).get("publicCollectibleType")
                if not node:
                    # errors present (e.g. "Entity not found" for comics/delisted) -> skip
                    return None
                return _map_node(node, uuid)
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            _maybe_disable_proxy(e)
        time.sleep(RETRY_BACKOFF * attempt)
    print(f"    enrich failed for {uuid}: {last_err}", flush=True)
    return None


def _map_node(n: Dict[str, Any], uuid: str) -> Dict[str, Any]:
    series = n.get("series") or {}
    brand = n.get("brand") or {}
    licensor = (brand.get("licensor") or {}) if isinstance(brand, dict) else {}
    img = n.get("image") or {}
    return {
        "veve_uuid": uuid,
        "image_url": img.get("webpUrl") or img.get("url"),
        "special_edition": n.get("isSpecialEdition"),
        "edition_type": n.get("editionType"),
        "description": n.get("description"),
        "veve_store_price": _num(n.get("storePrice")),
        "market_fee": _num(n.get("marketFee")),
        "daily_mcp_points": _num(n.get("dailyMcpPoints")),
        "drop_method": n.get("dropMethod"),
        "drop_date": n.get("dropDate"),
        "rarity_editions": _num(n.get("totalIssued")),
        "store_allocation": _num(n.get("totalStoreAllocation")),
        "sold_editions": _num(n.get("soldEditions")),
        "editions_in_circulation": _num(n.get("editionsInCirculation")),
        "burned_editions": _num(n.get("editionsBurnt")),
        "withheld_editions": _num(n.get("withheldEditions")),
        "first_available_edition": _num(n.get("firstAvailableEdition")),
        "veve_total_available": _num(n.get("totalAvailable")),
        "season": _num(series.get("season")),
        "is_blindbox": series.get("isBlindbox"),
        "veve_series_name": series.get("name"),
        "veve_brand": brand.get("name"),
        "veve_licensor": licensor.get("name"),
        "veve_enriched_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
    }


def fetch_comic(comic_id: str) -> Optional[Dict[str, Any]]:
    """Return comic-level enrichment columns for one VeVe comic id, or None."""
    payload = {
        "operationName": "publicStoreCollectibleEditionsQuery",
        "variables": {"id": comic_id},
        "query": COMIC_QUERY,
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().post(GRAPHQL_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                node = (data.get("data") or {}).get("publicComicType")
                if not node:
                    return None
                return _map_comic(node, comic_id)
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            _maybe_disable_proxy(e)
        time.sleep(RETRY_BACKOFF * attempt)
    print(f"    comic enrich failed for {comic_id}: {last_err}", flush=True)
    return None


def _map_comic(n: Dict[str, Any], comic_id: str) -> Dict[str, Any]:
    media = ((n.get("media") or {}).get("edges") or []) if isinstance(n.get("media"), dict) else []
    image_url = ""
    if media:
        node = (media[0] or {}).get("node") or {}
        image_url = node.get("url") or ""
    return {
        "comic_id": comic_id,
        "image_url": image_url,
        "description": n.get("description"),
        "veve_store_price": _num(n.get("storePrice")),
        "market_fee": _num(n.get("marketFee")),
        "daily_mcp_points": _num(n.get("dailyMcpPoints")),
        "drop_method": n.get("dropMethod"),
        "drop_date": n.get("dropDate"),
        "start_year": _num(n.get("startYear")),
        "rarity_editions": _num(n.get("totalIssued")),
        "sold_editions": _num(n.get("soldEditions")),
        "editions_in_circulation": _num(n.get("editionsInCirculation")),
        "burned_editions": _num(n.get("editionsBurnt")),
        "withheld_editions": _num(n.get("withheldEditions")),
        "first_available_edition": _num(n.get("firstAvailableEdition")),
        "veve_total_available": _num(n.get("totalAvailable")),
        "veve_comic_name": n.get("name"),
        "veve_enriched_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
    }


def enrich_comics(comic_ids: List[str], workers: int = DEFAULT_WORKERS) -> Dict[str, Dict[str, Any]]:
    """Enrich many VeVe comic ids concurrently. Returns {comic_id: columns}."""
    comic_ids = [c for c in dict.fromkeys(comic_ids) if c]
    total = len(comic_ids)
    if not total:
        return {}
    _decide_egress()
    via = "Apify residential proxy" if _use_proxy else "direct connection"
    print(f"Enriching {total} comics via VeVe GraphQL ({via})...", flush=True)
    out: Dict[str, Dict[str, Any]] = {}
    done = 0

    def task(c: str):
        time.sleep(PAUSE)
        return c, fetch_comic(c)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, c) for c in comic_ids]
        for fut in as_completed(futures):
            c, cols = fut.result()
            done += 1
            if cols:
                out[c] = cols
            if done % 250 == 0:
                print(f"    ... {done}/{total} comics processed ({len(out)} enriched)", flush=True)
    print(f"Comic enrichment done: {len(out)}/{total} comics enriched.", flush=True)
    return out


# Brand logo. CONFIRMED live (DevTools capture 2026-07-08): the brand logo is
# `brand.landscapeImage` (NOT `brand.image` — that guess returned HTTP 400).
# The licensor logo is NOT exposed on publicCollectibleType (only id/name); it
# needs the /brands licence-page query (captured separately) — so licensor_image
# stays empty here. This is an ISOLATED query: if a field differs, only this step
# fails (logged), the main enrichment is untouched.
BRAND_MEDIA_QUERY = (
    "query publicStoreCollectibleEditionsQuery($id: ID!){ "
    "publicCollectibleType(id:$id){ id "
    "brand{ id name landscapeImage{ url webpUrl transparentBackgroundUrl } "
    "licensor{ id name } } } }"
)


def fetch_brand_media(cid: str) -> Optional[Dict[str, Any]]:
    """Return {brand_uuid, brand_name, brand_image, licensor_*} for one collectible,
    or None (incl. when VeVe rejects the image field — that error is printed)."""
    payload = {"operationName": "publicStoreCollectibleEditionsQuery",
               "variables": {"id": cid}, "query": BRAND_MEDIA_QUERY}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().post(GRAPHQL_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                node = (data.get("data") or {}).get("publicCollectibleType")
                if not node:
                    if data.get("errors"):
                        print(f"    brand media query rejected (field mismatch?): "
                              f"{str(data['errors'])[:200]}", flush=True)
                    return None
                b = node.get("brand") or {}
                lz = (b.get("licensor") or {}) if isinstance(b, dict) else {}
                bi = (b.get("landscapeImage") or {}) if isinstance(b, dict) else {}
                return {
                    "brand_uuid": b.get("id"), "brand_name": b.get("name"),
                    "brand_image": bi.get("webpUrl") or bi.get("url"),
                    "licensor_uuid": lz.get("id"), "licensor_name": lz.get("name"),
                    "licensor_image": "",  # not on publicCollectibleType; needs licence-page query
                }
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            _maybe_disable_proxy(e)
        time.sleep(RETRY_BACKOFF * attempt)
    print(f"    brand media failed for {cid}: {last_err}", flush=True)
    return None


def enrich_brand_media(cids: List[str], workers: int = DEFAULT_WORKERS) \
        -> Dict[str, Dict[str, Any]]:
    """Fetch brand/licensor logos for representative collectible ids. Probes the
    first id; if VeVe doesn't return images (field mismatch), skips the rest so we
    never hammer a broken query. Returns {cid: media}."""
    cids = [c for c in dict.fromkeys(cids) if c]
    if not cids:
        return {}
    _decide_egress()
    probe = fetch_brand_media(cids[0])
    if not probe or not (probe.get("brand_image") or probe.get("licensor_image")):
        print("Brand media: VeVe returned no brand/licensor image on the probe "
              "(field name may differ) — skipping logo collection.", flush=True)
        return {cids[0]: probe} if probe else {}
    out: Dict[str, Dict[str, Any]] = {cids[0]: probe}

    def task(c: str):
        time.sleep(PAUSE)
        return c, fetch_brand_media(c)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(task, c) for c in cids[1:]]):
            c, m = fut.result()
            if m:
                out[c] = m
    print(f"Brand media: {len(out)}/{len(cids)} representatives fetched.", flush=True)
    return out


def enrich(uuids: List[str], workers: int = DEFAULT_WORKERS) -> Dict[str, Dict[str, Any]]:
    """Enrich many collectible uuids concurrently. Returns {uuid: columns}."""
    uuids = [u for u in dict.fromkeys(uuids) if u]  # dedupe, drop empties
    total = len(uuids)
    if not total:
        return {}
    _decide_egress()
    via = "Apify residential proxy" if _use_proxy else "direct connection"
    print(f"Enriching {total} collectibles via VeVe GraphQL ({via})...", flush=True)

    out: Dict[str, Dict[str, Any]] = {}
    done = 0

    def task(u: str):
        time.sleep(PAUSE)
        return u, fetch_collectible(u)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, u) for u in uuids]
        for fut in as_completed(futures):
            u, cols = fut.result()
            done += 1
            if cols:
                out[u] = cols
            if done % 250 == 0:
                print(f"    ... {done}/{total} processed ({len(out)} enriched)", flush=True)

    print(f"Enrichment done: {len(out)}/{total} collectibles enriched.", flush=True)
    return out


if __name__ == "__main__":
    import sys, json
    ids = sys.argv[1:] or ["8648d886-ed81-4ea1-bae7-cb1a0bc975bd"]
    res = enrich(ids)
    print(json.dumps(res, indent=2, ensure_ascii=False))


def _map_dynamic(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "veve_store_price": _num(n.get("storePrice")),
        "sold_editions": _num(n.get("soldEditions")),
        "editions_in_circulation": _num(n.get("editionsInCirculation")),
        "burned_editions": _num(n.get("editionsBurnt")),
        "withheld_editions": _num(n.get("withheldEditions")),
        "veve_total_available": _num(n.get("totalAvailable")),
        "store_allocation": _num(n.get("totalStoreAllocation")),
    }


def fetch_dynamic(item_id: str, is_comic: bool) -> Optional[Dict[str, Any]]:
    """Fetch only the variable (edition-count) fields for one item."""
    query = DYN_COMIC_QUERY if is_comic else DYN_COLLECTIBLE_QUERY
    root = "publicComicType" if is_comic else "publicCollectibleType"
    payload = {"operationName": "publicStoreCollectibleEditionsQuery",
               "variables": {"id": item_id}, "query": query}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().post(GRAPHQL_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                node = (r.json().get("data") or {}).get(root)
                return _map_dynamic(node) if node else None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            _maybe_disable_proxy(e)
        time.sleep(RETRY_BACKOFF * attempt)
    return None


def enrich_dynamic(item_ids: List[str], is_comic: bool,
                   workers: int = DEFAULT_WORKERS) -> Dict[str, Dict[str, Any]]:
    """Refresh variable fields for many items. Returns {id: dynamic_cols}."""
    item_ids = [i for i in dict.fromkeys(item_ids) if i]
    total = len(item_ids)
    if not total:
        return {}
    _decide_egress()
    kind = "comics" if is_comic else "collectibles"
    via = "Apify residential proxy" if _use_proxy else "direct connection"
    print(f"Refreshing variable fields for {total} {kind} ({via})...", flush=True)
    out: Dict[str, Dict[str, Any]] = {}
    done = 0

    def task(i: str):
        time.sleep(PAUSE)
        return i, fetch_dynamic(i, is_comic)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, i) for i in item_ids]
        for fut in as_completed(futures):
            i, cols = fut.result()
            done += 1
            if cols:
                out[i] = cols
            if done % 500 == 0:
                print(f"    ... {done}/{total} {kind} refreshed", flush=True)
    print(f"Variable-field refresh done: {len(out)}/{total} {kind}.", flush=True)
    return out


# ---------------------------------------------------------------------------
# FLOOR (VeVe's own secondary-market floor). COLLECTIBLES only.
# ---------------------------------------------------------------------------
# `floorMarketPrice` and `totalMarketListings` are exposed on publicCollectibleType.
# NOTE (confirmed live): VeVe only fills `floorMarketPrice` once a drop is SOLD OUT
# (totalAvailable == 0). While a collectible is still available in the store, the
# field is null — the caller then falls back to the my-nft-tracker floor.
FLOOR_QUERY = (
    "query publicStoreCollectibleEditionsQuery($id: ID!){ "
    "publicCollectibleType(id:$id){ id floorMarketPrice totalMarketListings "
    "totalAvailable storePrice } }"
)


def _map_floor(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "veve_floor_price": _num(n.get("floorMarketPrice")),
        "veve_total_market_listings": _num(n.get("totalMarketListings")),
        "veve_total_available": _num(n.get("totalAvailable")),
        "veve_store_price": _num(n.get("storePrice")),
    }


def fetch_floor(uuid: str) -> Optional[Dict[str, Any]]:
    """VeVe floor + listings for one collectible uuid (None if not found)."""
    payload = {"operationName": "publicStoreCollectibleEditionsQuery",
               "variables": {"id": uuid}, "query": FLOOR_QUERY}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().post(GRAPHQL_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                node = (r.json().get("data") or {}).get("publicCollectibleType")
                return _map_floor(node) if node else None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            _maybe_disable_proxy(e)
        time.sleep(RETRY_BACKOFF * attempt)
    print(f"    floor fetch failed for {uuid}: {last_err}", flush=True)
    return None


def enrich_floors(uuids: List[str], workers: int = DEFAULT_WORKERS) \
        -> Dict[str, Dict[str, Any]]:
    """Fetch the VeVe floor for many collectibles. Returns {uuid: floor_cols}."""
    uuids = [u for u in dict.fromkeys(uuids) if u]
    total = len(uuids)
    if not total:
        return {}
    _decide_egress()
    via = "Apify residential proxy" if _use_proxy else "direct connection"
    print(f"Fetching VeVe floor for {total} collectibles ({via})...", flush=True)
    out: Dict[str, Dict[str, Any]] = {}
    done = 0

    def task(u: str):
        time.sleep(PAUSE)
        return u, fetch_floor(u)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, u) for u in uuids]
        for fut in as_completed(futures):
            u, cols = fut.result()
            done += 1
            if cols:
                out[u] = cols
            if done % 500 == 0:
                print(f"    ... {done}/{total} floors fetched", flush=True)
    print(f"VeVe floor fetch done: {len(out)}/{total} collectibles.", flush=True)
    return out
