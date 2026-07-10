"""
Tracker BURNS OMI — StackR (Base L2).

Les OMI depenses sur StackR sont collectes sur l'adresse StackR
0x821c1ed723c3148eb74540b1201ea3369c910c17 (Base L2) puis retires vers Ethereum
L1 et brules (-> 0x0) chaque semaine. Le "burn quotidien" = les OMI DEPOSES sur
cette adresse chaque jour (ce qui sera detruit). Source : explorer Blockscout
OUVERT de Base (base.blockscout.com, pas de cle).

Produit :
    data/burns_daily.csv     date, source, transactions, omi_burned  (commite)
    data/burns_state.json     curseur (dernier bloc traite, resumable)
    onglet 🔥H-BURNS          date, source, transactions, omi_burned, cumulative

Resumable : 1er run = backfill complet (pagination), runs suivants = seulement
les nouveaux depots depuis le dernier bloc vu. Dates en PT (fuseau metier VeVe).

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID,
      BURNS_MAX_PAGES (0 = illimite), BURNS_PAUSE (defaut 0.1).
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

from scraper.sheets import _client, _open_worksheet, append_log

BASE_BS = "https://base.blockscout.com"
STACKR_ADDR = "0x821c1ed723c3148eb74540b1201ea3369c910c17"
OMI_SYMBOL = "OMI"
PT = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "veve-omi-burns/1.0", "Accept": "application/json"}

DATA_DIR = os.environ.get("BURNS_DATA_DIR", "data")
DAILY_CSV = os.path.join(DATA_DIR, "burns_daily.csv")
STATE_JSON = os.path.join(DATA_DIR, "burns_state.json")
BURNS_TAB = "🔥H-BURNS"
HEADER = ["date", "source", "transactions", "omi_burned", "cumulative"]
DAILY_HEADER = ["date", "source", "transactions", "omi_burned"]


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
    """'2026-07-10T06:56:11.000000Z' -> date PT."""
    s = iso_ts.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        dt = _dt.datetime.strptime(iso_ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.timezone.utc)
    return dt.astimezone(PT).strftime("%Y-%m-%d")


def fetch_deposits(stop_block):
    """Depots OMI entrants sur l'adresse StackR (Base), du plus recent au plus
    vieux, jusqu'au bloc `stop_block` (exclu) ou epuisement.
    Retourne (par_jour {date:[tx,omi]}, newest_block, pages)."""
    max_pages = int(os.environ.get("BURNS_MAX_PAGES", "0"))
    pause = float(os.environ.get("BURNS_PAUSE", "0.1"))
    url = f"{BASE_BS}/api/v2/addresses/{STACKR_ADDR}/token-transfers"
    params = {"type": "ERC-20"}
    per_day = defaultdict(lambda: [0, 0.0])   # date -> [n_tx, omi]
    newest_block = stop_block or 0
    pages = 0
    seen_newest = False
    while True:
        data = _get(url, params)
        items = data.get("items") or []
        if not items:
            break
        stop = False
        for it in items:
            blk = it.get("block_number") or (it.get("block") if isinstance(it.get("block"), int) else None)
            try:
                blk = int(blk)
            except (TypeError, ValueError):
                blk = None
            to = ((it.get("to") or {}).get("hash") or "").lower()
            sym = (it.get("token") or {}).get("symbol")
            if not seen_newest and blk:
                newest_block = max(newest_block, blk)
            if stop_block and blk is not None and blk <= stop_block:
                stop = True
                break
            if to == STACKR_ADDR and sym == OMI_SYMBOL:
                tot = it.get("total")
                raw = tot.get("value") if isinstance(tot, dict) else it.get("value")
                try:
                    omi = int(raw) / 1e18
                except (TypeError, ValueError):
                    omi = 0.0
                d = _pt_date(it.get("timestamp") or "")
                if d:
                    per_day[d][0] += 1
                    per_day[d][1] += omi
        seen_newest = True
        pages += 1
        nxt = data.get("next_page_params")
        if stop or not nxt or (max_pages and pages >= max_pages):
            break
        params = {"type": "ERC-20", **nxt}
        if pause:
            time.sleep(pause)
    return per_day, newest_block, pages


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
    with open(DAILY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(DAILY_HEADER)
        for (d, src) in sorted(rows):
            tx, omi = rows[(d, src)]
            w.writerow([d, src, tx, round(omi, 2)])


def _load_state():
    if os.path.exists(STATE_JSON):
        with open(STATE_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_JSON) or ".", exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def _write_sheet(sheet_id, rows):
    sh = _client().open_by_key(sheet_id)
    ws = _open_worksheet(sh, BURNS_TAB, cols=len(HEADER))
    grid = [HEADER]
    cumul = defaultdict(float)
    for (d, src) in sorted(rows):
        tx, omi = rows[(d, src)]
        cumul[src] += omi
        grid.append([d, src, tx, round(omi, 2), round(cumul[src], 2)])
    ws.clear()
    for i in range(0, len(grid), 20000):
        if i == 0:
            ws.update(range_name="A1", values=grid[:20000], value_input_option="RAW")
        else:
            ws.append_rows(grid[i:i + 20000], value_input_option="RAW")
    try:
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERREUR : SHEET_ID requis.", file=sys.stderr)
        return 2

    state = _load_state()
    stop_block = state.get("newest_block")  # None au 1er run -> backfill complet
    print(f"StackR burns : {'incremental depuis bloc ' + str(stop_block) if stop_block else 'BACKFILL complet'}...",
          flush=True)

    per_day, newest_block, pages = fetch_deposits(stop_block)
    print(f"{pages} pages, {sum(v[0] for v in per_day.values())} depots, "
          f"newest_block={newest_block}.", flush=True)

    daily = _load_daily()
    for d, (tx, omi) in per_day.items():
        key = (d, "StackR")
        cur = daily.get(key, [0, 0.0])
        daily[key] = [cur[0] + tx, cur[1] + omi]
    _save_daily(daily)

    _write_sheet(sheet_id, daily)

    if newest_block:
        state["newest_block"] = newest_block
        state["updated_at"] = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state)

    total = sum(v[1] for k, v in daily.items() if k[1] == "StackR")
    summary = {"status": "OK", "pages": pages, "days": len(per_day),
               "stackr_cumulative_omi": round(total, 0),
               "duration": f"{time.time()-t0:.0f}s"}
    try:
        append_log(sheet_id, "burns", "OK",
                   "; ".join(f"{k}={v}" for k, v in summary.items() if k != "status"))
    except Exception as e:
        print(f"log warning: {e}", flush=True)
    print(f"Done. {summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
