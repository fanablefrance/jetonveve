"""
Tracker BURNS OMI — StackR (Base L2). Autonome + RESUMABLE (repo jetonveve).

Les OMI depenses sur StackR sont collectes sur 0x821c (Base L2) puis brules.
Le "burn quotidien" = les OMI DEPOSES sur cette adresse chaque jour. Source :
Blockscout ouvert de Base (base.blockscout.com, pas de cle) — mais LENT
(~4 s/page), d'ou un backfill RESUMABLE par tranches avec auto-relance.

Fichiers (commites) :
    data/burns_daily.csv    date, source, transactions, omi_burned, cumulative
    data/burns_state.json   backfill_done, next_page (curseur pagination),
                            newest_block (pour l'incremental), pages, updated_at

Marche :
  - backfill (1ere fois) : pagine du present vers le passe par tranches de
    BURNS_MINUTES, checkpoint tous les 50 pages (sauve CSV + etat), se relance
    jusqu'a epuisement -> backfill_done=true.
  - incremental (backfill fini) : ne recupere que les depots > newest_block.

Dates en PT. Env : BURNS_MINUTES (25), BURNS_PAUSE (0), BURNS_DATA_DIR (data).
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import sys
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests

BASE_BS = "https://base.blockscout.com"
STACKR_ADDR = "0x821c1ed723c3148eb74540b1201ea3369c910c17"
OMI_SYMBOL = "OMI"
PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "veve-omi-burns/1.0", "Accept": "application/json"}

DATA_DIR = os.environ.get("BURNS_DATA_DIR", "data")
DAILY_CSV = os.path.join(DATA_DIR, "burns_daily.csv")
STATE_JSON = os.path.join(DATA_DIR, "burns_state.json")
DAILY_HEADER = ["date", "source", "transactions", "omi_burned", "cumulative"]
TRANSFERS_URL = f"{BASE_BS}/api/v2/addresses/{STACKR_ADDR}/token-transfers"
CHECKPOINT_PAGES = 50


def _get(url, params=None):
    last = None
    for attempt in range(1, 6):
        try:
            r = requests.get(url, params=params or {}, headers=UA, timeout=40)
            if r.status_code == 429:
                time.sleep(3 * attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"echec {url}: {last}")


def _pt_date(iso_ts: str) -> str:
    s = (iso_ts or "").replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(s).astimezone(PT).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# --- CSV agrege (par jour) : charge en [tx, omi] et reecrit avec cumulative ---

def _load_daily():
    rows = {}
    if os.path.exists(DAILY_CSV):
        with open(DAILY_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["date"], r["source"])] = [int(r["transactions"]),
                                                  float(r["omi_burned"])]
    return rows


def _save_daily(rows):
    os.makedirs(os.path.dirname(DAILY_CSV) or ".", exist_ok=True)
    cumul = defaultdict(float)
    tmp = DAILY_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(DAILY_HEADER)
        for (d, src) in sorted(rows):
            tx, omi = rows[(d, src)]
            cumul[src] += omi
            w.writerow([d, src, tx, round(omi, 2), round(cumul[src], 2)])
    os.replace(tmp, DAILY_CSV)


def _load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_JSON) or ".", exist_ok=True)
    state["updated_at"] = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATE_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_JSON)


def _accumulate(daily, items, stop_block):
    """Agrege les depots OMI->STACKR d'une page dans `daily`. Retourne
    (n_deposits, newest_block_de_la_page, stop_atteint)."""
    n, newest, stop = 0, 0, False
    for it in items:
        try:
            blk = int(it.get("block_number"))
        except (TypeError, ValueError):
            blk = None
        if blk:
            newest = max(newest, blk)
        if stop_block and blk is not None and blk <= stop_block:
            stop = True
            break
        to = ((it.get("to") or {}).get("hash") or "").lower()
        sym = (it.get("token") or {}).get("symbol")
        if to == STACKR_ADDR and sym == OMI_SYMBOL:
            tot = it.get("total")
            raw = tot.get("value") if isinstance(tot, dict) else it.get("value")
            try:
                omi = int(raw) / 1e18
            except (TypeError, ValueError):
                omi = 0.0
            d = _pt_date(it.get("timestamp"))
            if d:
                cur = daily.get((d, "StackR"), [0, 0.0])
                daily[(d, "StackR")] = [cur[0] + 1, cur[1] + omi]
                n += 1
    return n, newest, stop


def run_backfill(state, daily, budget_s):
    """Pagine present->passe par tranches. Met a jour state/daily en place."""
    t0 = time.time()
    params = state.get("next_page") or {"type": "ERC-20"}
    first_newest = state.get("newest_block") or 0
    max_pages = int(os.environ.get("BURNS_MAX_PAGES", "0"))
    pages = 0
    deposits = 0
    while True:
        if time.time() - t0 > budget_s:
            print(f"Budget temps atteint ({budget_s/60:.0f} min).", flush=True)
            break
        if max_pages and pages >= max_pages:
            print(f"Budget pages atteint ({max_pages}).", flush=True)
            break
        data = _get(TRANSFERS_URL, params)
        items = data.get("items") or []
        if not items:
            state["backfill_done"] = True
            state["next_page"] = None
            print("Plus d'items — BACKFILL TERMINE.", flush=True)
            break
        n, newest, _ = _accumulate(daily, items, None)
        deposits += n
        if not state.get("newest_block") and newest:
            first_newest = newest
            state["newest_block"] = newest      # fige le point de reprise incremental
        pages += 1
        state["pages"] = int(state.get("pages", 0)) + 1
        nxt = data.get("next_page_params")
        if pages % CHECKPOINT_PAGES == 0:
            state["next_page"] = nxt
            _save_daily(daily)
            _save_state(state)
            print(f"    checkpoint : {state['pages']} pages cumulees, "
                  f"{deposits} depots ce run.", flush=True)
        if not nxt:
            state["backfill_done"] = True
            state["next_page"] = None
            print("Fin de pagination — BACKFILL TERMINE.", flush=True)
            break
        params = {"type": "ERC-20", **nxt}
        pause = float(os.environ.get("BURNS_PAUSE", "0"))
        if pause:
            time.sleep(pause)
    state["next_page"] = state.get("next_page") if not state.get("backfill_done") else None
    return pages, deposits


def run_incremental(state, daily):
    """Recupere les nouveaux depots > newest_block (backfill deja fini)."""
    stop_block = state.get("newest_block") or 0
    params = {"type": "ERC-20"}
    pages, deposits, run_newest, seen = 0, 0, stop_block, False
    while True:
        data = _get(TRANSFERS_URL, params)
        items = data.get("items") or []
        if not items:
            break
        n, newest, stop = _accumulate(daily, items, stop_block)
        deposits += n
        if not seen and newest:
            run_newest = max(run_newest, newest)
            seen = True
        pages += 1
        nxt = data.get("next_page_params")
        if stop or not nxt:
            break
        params = {"type": "ERC-20", **nxt}
    if run_newest:
        state["newest_block"] = run_newest
    return pages, deposits


def main() -> int:
    t0 = time.time()
    budget_s = float(os.environ.get("BURNS_MINUTES", "25")) * 60
    state = _load_state()
    daily = _load_daily()

    if not state.get("backfill_done"):
        print(f"BACKFILL (resumable) : pages deja faites={state.get('pages', 0)}, "
              f"reprise={'oui' if state.get('next_page') else 'debut'}...", flush=True)
        pages, deposits = run_backfill(state, daily, budget_s)
    else:
        print(f"INCREMENTAL depuis bloc {state.get('newest_block')}...", flush=True)
        pages, deposits = run_incremental(state, daily)

    _save_daily(daily)
    _save_state(state)

    stackr = {k[0]: v for k, v in daily.items() if k[1] == "StackR"}
    total = sum(v[1] for v in stackr.values())
    print(f"\n=== RECAP StackR : {len(stackr)} jours, cumul {total:,.0f} OMI. "
          f"backfill_done={state.get('backfill_done', False)} "
          f"({pages} pages / {deposits} depots ce run, {time.time()-t0:.0f}s)",
          flush=True)
    for d in sorted(stackr)[-8:]:
        tx, omi = stackr[d]
        print(f"   {d} | {tx:>4} tx | {omi:>15,.0f} OMI", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
