#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""catalog_export — catalogue exploitable HORS Sheet (preda -> Release jetonveve).

Exporte le referentiel des items (uuid -> nom, rarete, serie, marque, tirage,
store price, floor du jour) en 1 CSV.gz, publie ensuite en Release `catalogue`
sur jetonveve (public) par le workflow. But : que l'entrepot soit AUTOSUFFISANT
— alertes, chatbot et service de tracking n'ont plus besoin d'un acces Google
pour connaitre les noms/prix ; ils joignent par uuid avec analytics-derived
et transfers.parquet.

Lecture des colonnes PAR NOM au runtime (robuste si l'ordre des colonnes du
Sheet change — meme pattern que fiche.py). Sources : 🔵C-COLLECTIBLE +
🟢C-COMICS (froid) + _DynState (floor/listings/store du jour).

Sortie : catalogue.csv.gz — header :
  uuid,kind,name,edition_type,rarity,release_date,series,brand,licensor,
  tirage,store_price,floor,listings,ath,atl

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID (comme le daily),
      EXPECTED_MIN_ITEMS (defaut 15000, garde-fou exit 1),
      CATALOG_OUT (defaut catalogue.csv.gz).
"""
import csv
import gzip
import os
import sys
import time

from scraper.sheets import _client

COLLECT_TAB = "🔵C-COLLECTIBLE"
COMICS_TAB = "🟢C-COMICS"
DYN_STATE_TAB = "_DynState"

# nom de sortie -> nom de colonne dans le Sheet (froid)
COLD_MAP = [
    ("uuid", "veve_uuid"), ("kind", "category"), ("name", "name"),
    ("edition_type", "edition_type"), ("rarity", "rarity"),
    ("release_date", "releaseDate"), ("series", "veve_series_name"),
    ("brand", "veve_brand"), ("licensor", "veve_licensor"),
    ("tirage", "supply"), ("store_price", "store_price_gems"),
    ("ath", "ath"), ("atl", "atl"),
]
# nom de sortie -> colonne _DynState (chaud, floor du jour)
DYN_MAP = [("floor", "market_lowestOffer"), ("listings", "market_totalListings")]

HEADER = [o for o, _ in COLD_MAP[:11]] + ["floor", "listings", "ath", "atl"]


def _retry(fn, tries=5):
    """Backoff 429/503 (meme lecon que export_elements : ne pas avaler)."""
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:                      # noqa: BLE001
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code not in (429, 503) or i == tries - 1:
                raise
            wait = [15, 30, 45, 60][min(i, 3)]
            print(f"  {code} Google — retry dans {wait}s ({i + 1}/{tries})")
            time.sleep(wait)
    return None


def _rows_by_name(ws) -> list:
    """get_all_values -> liste de dicts {nom de colonne: valeur}."""
    values = _retry(ws.get_all_values)
    if not values:
        return []
    head = values[0]
    return [dict(zip(head, r)) for r in values[1:] if any(r)]


def main() -> None:
    out = os.environ.get("CATALOG_OUT", "catalogue.csv.gz")
    expected = int(os.environ.get("EXPECTED_MIN_ITEMS") or 15_000)
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERROR: SHEET_ID requis.", file=sys.stderr)
        sys.exit(1)
    sh = _client().open_by_key(sheet_id)

    # ── floor/listings du jour (facultatif : on exporte meme sans) ──────────
    dyn = {}
    try:
        for r in _rows_by_name(sh.worksheet(DYN_STATE_TAB)):
            uid = (r.get("veve_uuid") or "").strip()
            if uid:
                dyn[uid] = {o: (r.get(c) or "") for o, c in DYN_MAP}
        print(f"_DynState : {len(dyn)} uuids (floor/listings)")
    except Exception as exc:                          # noqa: BLE001
        print(f"⚠️ _DynState illisible ({exc}) — catalogue exporte SANS floors.")

    # ── catalogue froid (collectibles + comics) ─────────────────────────────
    items, seen = [], set()
    for tab, default_kind in ((COLLECT_TAB, "collectible"), (COMICS_TAB, "comic")):
        rows = _rows_by_name(sh.worksheet(tab))
        n = 0
        for r in rows:
            uid = (r.get("veve_uuid") or "").strip()
            if not uid or uid in seen:
                continue
            seen.add(uid)
            rec = {o: (r.get(c) or "") for o, c in COLD_MAP}
            rec["uuid"] = uid
            rec["kind"] = rec["kind"] or default_kind
            rec.update(dyn.get(uid, {o: "" for o, _ in DYN_MAP}))
            items.append(rec)
            n += 1
        print(f"{tab} : {n} items")

    if len(items) < expected:
        print(f"ERREUR GARDE-FOU : {len(items)} items < {expected} — "
              "lecture Sheet incomplete ? Release NON mise a jour.")
        sys.exit(1)

    with gzip.open(out, "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(items)
    with_floor = sum(1 for i in items if i.get("floor"))
    print(f"✅ {out} : {len(items)} items ({with_floor} avec floor).")


if __name__ == "__main__":
    main()
