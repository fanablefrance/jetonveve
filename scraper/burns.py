"""
Tracker BURNS OMI — StackR (Base L2). Version AUTONOME (repo jetonveve).

Les OMI depenses sur StackR sont collectes sur 0x821c (Base L2) puis retires
vers Ethereum L1 et brules chaque semaine. Le "burn quotidien" = les OMI
DEPOSES sur cette adresse chaque jour. Source : Blockscout ouvert de Base
(base.blockscout.com, pas de cle).

Ecrit UNIQUEMENT des fichiers (pas de Google Sheet pendant le rodage) :
    data/burns_daily.csv    date, source, transactions, omi_burned, cumulative
    data/burns_state.json   curseur (dernier bloc traite, resumable)

Resumable : 1er run = backfill complet, runs suivants = nouveaux depots.
Dates en PT (fuseau metier VeVe).

Env : BURNS_MAX_PAGES (0 = illimite), BURNS_PAUSE (defaut 0.1).
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


def _get(url, params=None):
    last = None
    for attempt in range(1, 5):
        try:
            r = requests.get(url, params=params or {}, headers=UA, timeout=40)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"echec {url}: {last}")


def _pt_date(iso_ts: str) -> str:
    s = (iso_ts or "").replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return ""
    return dt.astimezone(PT).strftime("%Y-%m-%d")


def fetch_deposits(stop_block):
    """Depots OMI entrants sur StackR (Base), newest-first, jusqu'a stop_block."""
    max_pages = int(os.environ.get("BURNS_MAX_PAGES", "0"))
    pause = float(os.environ.get("BURNS_PAUSE", "0.1"))
    url = f"{BASE_BS}/api/v2/addresses/{STACKR_ADDR}/token-transfers"
    params = {"type": "ERC-20"}
    per_day = defaultdict(lambda: [0, 0.0])
    newest_block = stop_block or 0
    pages, seen_newest, deposits = 0, False, 0
    while True:
        data = _get(url, params)
        items = data.get("items") or []
        if not items:
            break
        stop = False
        for it in items:
            try:
                blk = int(it.get("block_number"))
            except (TypeError, ValueError):
                blk = None
            if not seen_newest and blk:
                newest_block = max(newest_block, blk)
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
                    per_day[d][0] += 1
                    per_day[d][1] += omi
                    deposits += 1
        seen_newest = True
        pages += 1
        if pages % 100 == 0:
            print(f"    ... {pages} pages, {deposits} depots", flush=True)
        nxt = data.get("next_page_params")
        if stop or not nxt or (max_pages and pages >= max_pages):
            break
        params = {"type": "ERC-20", **nxt}
        if pause:
            time.sleep(pause)
    return per_day, newest_block, pages, deposits


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
    with open(DAILY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(DAILY_HEADER)
        for (d, src) in sorted(rows):
            tx, omi = rows[(d, src)]
            cumul[src] += omi
            w.writerow([d, src, tx, round(omi, 2), round(cumul[src], 2)])


def _load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_JSON) or ".", exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def main() -> int:
    t0 = time.time()
    state = _load_state()
    stop_block = state.get("newest_block")
    print(f"StackR burns : {'incremental depuis bloc ' + str(stop_block) if stop_block else 'BACKFILL complet (~74k transferts)'}...",
          flush=True)

    per_day, newest_block, pages, deposits = fetch_deposits(stop_block)
    print(f"{pages} pages, {deposits} depots, newest_block={newest_block}.", flush=True)

    daily = _load_daily()
    for d, (tx, omi) in per_day.items():
        key = (d, "StackR")
        cur = daily.get(key, [0, 0.0])
        daily[key] = [cur[0] + tx, cur[1] + omi]
    _save_daily(daily)

    if newest_block:
        state["newest_block"] = newest_block
        state["updated_at"] = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state)

    stackr = {k[0]: v for k, v in daily.items() if k[1] == "StackR"}
    total = sum(v[1] for v in stackr.values())
    print(f"\n=== RECAP StackR : {len(stackr)} jours, cumul {total:,.0f} OMI brules.",
          flush=True)
    print("   10 derniers jours (date | tx | OMI/jour) :", flush=True)
    for d in sorted(stackr)[-10:]:
        tx, omi = stackr[d]
        print(f"   {d} | {tx:>4} | {omi:>15,.0f}", flush=True)
    print(f"\nDone en {time.time()-t0:.0f}s. Fichier : {DAILY_CSV}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
