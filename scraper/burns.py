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

GOOGLE SHEET (v5, fix 2026-07-10) : si les secrets GOOGLE_SERVICE_ACCOUNT_JSON
et SHEET_ID sont presents, reecrit l'onglet 🔥H-BURNS a chaque run. Les
montants sont ecrits en NOMBRES NATIFS avec value_input_option=RAW — jamais en
chaines "123.45" : le Sheet est en locale FR et l'ecriture interpretee avalait
le point decimal comme separateur de milliers (1 374 227.22 -> 137 422 722,
x100 dans l'onglet alors que le CSV etait juste). Tolerant : sans secrets ou
en cas d'echec, le CSV fait foi.

DECOMPO NFT (v6, 2026-07-11) : chaque depot sur 0x821c vient du contrat
Marketplace StackR (0x61e7c72569...) via des tx `settle`. Anatomie verifiee
on-chain : VENTE NFT = acheteur paie P -> vendeur ~94,1 %, frais ~3,9 %
(0x00d438...), BURN = 1,96 % de P. On pagine les SORTIES du Marketplace
(filter=from), on regroupe par tx, et on compte les settles avec payout
(= ventes NFT : burn 2 % + volume de vente). Resumable (split_* dans l'etat,
groupe partiel de frontiere sauvegarde).

GEMS (v7, 2026-07-12) : les achats de gems en OMI ("OMI to Gem", feature
StackR depuis le 19/11/2025, 1 gem = 1 $ d'OMI) ne passent PAS par 0x821c.
Circuit verifie on-chain : le wallet user envoie 100 % au contrat OmiToGems
(0xe195b898...) qui redistribue chaque conversion en 3 sorties : 90 % tresor
VeVe (0x22f097eb...), 7 % BURN accumule sur 0x52a6d705..., 3 % frais StackR
(0xc03ed268...). Le burn gems quotidien = la somme des transferts
OmiToGems -> 0x52a6d705 du jour (valide a 1 OMI pres contre le tracker
communautaire "OMI to GEM" le 09/07). Walk dedie RESUMABLE (gem_* dans
l'etat, backfill ~19/11/2025 = quelques dizaines de pages) qui remplit
gem_buys / omi_gem dans burns_split_daily.csv + 🔥H-BURNS. L'ancienne
classification "settle sans payout = GEM" (v6) est retiree : ces settles
n'existent pas en pratique et doublonneraient le walk dedie.

Sorties :
    data/burns_split_daily.csv  date, nft_sales, omi_nft, omi_volume,
                                gem_buys, omi_gem
    🔥H-BURNS etendu : + nft_sales, omi_nft, gem_buys, omi_gem, omi_volume_nft

Dates en PT. Env : BURNS_MINUTES (25), BURNS_PAUSE (0), BURNS_DATA_DIR (data),
BURNS_MAX_PAGES / BURNS_SPLIT_MAX_PAGES / BURNS_GEM_MAX_PAGES (caps de test).
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
BURNS_TAB = "\U0001F525H-BURNS"

# ---- decompo NFT (v6) ----
MARKETPLACE_ADDR = "0x61e7c72569b3145e1fbeac3704ddb5e66d24a6f5"
MP_URL = f"{BASE_BS}/api/v2/addresses/{MARKETPLACE_ADDR}/token-transfers"
SPLIT_CSV = os.path.join(DATA_DIR, "burns_split_daily.csv")
SPLIT_HEADER = ["date", "nft_sales", "omi_nft", "omi_volume",
                "gem_buys", "omi_gem"]
SHEET_HEADER = DAILY_HEADER + ["nft_sales", "omi_nft", "gem_buys",
                               "omi_gem", "omi_volume_nft"]

# ---- gems OMI to Gem (v7) ----
OMITOGEMS_ADDR = "0xe195b8985a0fac337b368e38fefb9e80e75aa33a"
GEM_SINK_ADDR = "0x52a6d705ef28aca50b19b90598241a67ace8a772"
GEM_URL = f"{BASE_BS}/api/v2/addresses/{GEM_SINK_ADDR}/token-transfers"


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
            # v6 : curseur EXACT (la prochaine page a traiter) — avant, on
            # repartait du dernier checkpoint et on recomptait jusqu'a 49
            # pages par tranche.
            state["next_page"] = params
            print(f"Budget temps atteint ({budget_s/60:.0f} min).", flush=True)
            break
        if max_pages and pages >= max_pages:
            state["next_page"] = params
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


# ---------------------------------------------------------------------------
# DECOMPO NFT — pagination des sorties du Marketplace, groupees par tx
# ---------------------------------------------------------------------------

def _load_split():
    rows = {}
    if os.path.exists(SPLIT_CSV):
        with open(SPLIT_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = [int(r["nft_sales"]), float(r["omi_nft"]),
                                   float(r["omi_volume"]), int(r["gem_buys"]),
                                   float(r["omi_gem"])]
    return rows


def _save_split(rows):
    os.makedirs(os.path.dirname(SPLIT_CSV) or ".", exist_ok=True)
    tmp = SPLIT_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(SPLIT_HEADER)
        for d in sorted(rows):
            n, omi_n, vol, g, omi_g = rows[d]
            w.writerow([d, n, round(omi_n, 2), round(vol, 2),
                        g, round(omi_g, 2)])
    os.replace(tmp, SPLIT_CSV)


def _finalize_group(split, group):
    """Compte une tx settle complete avec payout (= vente NFT).
    v7 : les settles SANS payout ne sont plus classes GEM (jamais observes
    en pratique — les gems ont leur circuit dedie OmiToGems, walk run_gems)."""
    if not group or not group.get("outs"):
        return
    d = group.get("date") or ""
    if not d:
        return
    burn = sum(v for a, v in group["outs"] if a == STACKR_ADDR)
    payout = sum(v for a, v in group["outs"] if a != STACKR_ADDR)
    if payout <= 0:
        return
    cur = split.get(d, [0, 0.0, 0.0, 0, 0.0])
    split[d] = [cur[0] + 1, cur[1] + burn, cur[2] + burn + payout,
                cur[3], cur[4]]


def _split_walk(state, split, budget_s, incremental):
    """Pagine les sorties OMI du Marketplace. Regroupe par tx (transferts
    contigus), classe NFT. Resumable : split_next_page + groupe partiel
    de frontiere dans split_pending (une tx peut chevaucher deux pages)."""
    t0 = time.time()
    stop_block = state.get("split_newest_block") or 0 if incremental else 0
    params = ({"type": "ERC-20", "filter": "from"}
              if incremental or not state.get("split_next_page")
              else {"type": "ERC-20", "filter": "from",
                    **state["split_next_page"]})
    group = None if incremental else state.get("split_pending")
    max_pages = int(os.environ.get("BURNS_SPLIT_MAX_PAGES", "0"))
    pages, txs, run_newest, seen_top = 0, 0, 0, False
    stopped = False
    while True:
        if not incremental and time.time() - t0 > budget_s:
            state["split_next_page"] = params
            state["split_pending"] = group
            print(f"    decompo : budget temps atteint.", flush=True)
            return pages, txs
        if max_pages and pages >= max_pages:
            if not incremental:
                state["split_next_page"] = params
                state["split_pending"] = group
            return pages, txs
        data = _get(MP_URL, params)
        items = data.get("items") or []
        if not items:
            if not incremental:
                state["split_done"] = True
                state["split_next_page"] = None
                print("    DECOMPO : backfill TERMINE.", flush=True)
            stopped = True
            break
        for it in items:
            frm = ((it.get("from") or {}).get("hash") or "").lower()
            sym = (it.get("token") or {}).get("symbol")
            if frm != MARKETPLACE_ADDR or sym != OMI_SYMBOL:
                continue
            try:
                blk = int(it.get("block_number"))
            except (TypeError, ValueError):
                blk = None
            if blk and not seen_top:
                run_newest = max(run_newest, blk)
                seen_top = True
                if not incremental and not state.get("split_newest_block"):
                    # fige le point de reprise incremental des le 1er passage
                    state["split_newest_block"] = blk
            if incremental and stop_block and blk is not None \
                    and blk <= stop_block:
                stopped = True
                break
            h = it.get("transaction_hash") or it.get("tx_hash") or ""
            to = ((it.get("to") or {}).get("hash") or "").lower()
            tot = it.get("total")
            raw = tot.get("value") if isinstance(tot, dict) else it.get("value")
            try:
                omi = int(raw) / 1e18
            except (TypeError, ValueError):
                omi = 0.0
            if group is not None and group.get("tx") != h:
                _finalize_group(split, group)
                txs += 1
                group = None
            if group is None:
                group = {"tx": h, "date": _pt_date(it.get("timestamp")),
                         "outs": []}
            group["outs"].append([to, omi])
        pages += 1
        if stopped:
            break
        nxt = data.get("next_page_params")
        if not incremental:
            state["split_pages"] = int(state.get("split_pages", 0)) + 1
            if pages % CHECKPOINT_PAGES == 0:
                state["split_next_page"] = nxt
                state["split_pending"] = group
                _save_split(split)
                _save_state(state)
                print(f"    decompo checkpoint : {state['split_pages']} pages "
                      f"cumulees, {txs} settles ce run.", flush=True)
        if not nxt:
            if not incremental:
                state["split_done"] = True
                state["split_next_page"] = None
                print("    DECOMPO : fin de pagination — backfill TERMINE.",
                      flush=True)
            stopped = True
            break
        params = {"type": "ERC-20", "filter": "from", **nxt}
        pause = float(os.environ.get("BURNS_PAUSE", "0"))
        if pause:
            time.sleep(pause)
    # frontiere : en fin de backfill/incremental le dernier groupe est complet
    # (fin de pagination ou stop_block) ; sinon il repart dans split_pending.
    if stopped and group is not None:
        _finalize_group(split, group)
        txs += 1
        group = None
    if not incremental:
        state["split_pending"] = group
        if state.get("split_done"):
            state["split_pending"] = None
    if incremental and run_newest:
        state["split_newest_block"] = run_newest
    return pages, txs


def run_split(state, split, budget_s):
    """Backfill resumable puis incremental de la decompo NFT."""
    if not state.get("split_done"):
        print(f"DECOMPO NFT (backfill resumable) : pages deja faites="
              f"{state.get('split_pages', 0)}...", flush=True)
        return _split_walk(state, split, budget_s, incremental=False)
    print(f"DECOMPO NFT incremental depuis bloc "
          f"{state.get('split_newest_block')}...", flush=True)
    return _split_walk(state, split, budget_s, incremental=True)


# ---------------------------------------------------------------------------
# GEMS (v7) — entrees de l'accumulateur burn 7 % (OmiToGems -> 0x52a6d705)
# ---------------------------------------------------------------------------

def _gem_accumulate(split, items, stop_block):
    """Agrege les transferts OmiToGems -> GEM_SINK d'une page dans `split`
    (colonnes gem_buys / omi_gem). Retourne (n, newest, stop)."""
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
        frm = ((it.get("from") or {}).get("hash") or "").lower()
        to = ((it.get("to") or {}).get("hash") or "").lower()
        sym = (it.get("token") or {}).get("symbol")
        if frm == OMITOGEMS_ADDR and to == GEM_SINK_ADDR and sym == OMI_SYMBOL:
            tot = it.get("total")
            raw = tot.get("value") if isinstance(tot, dict) else it.get("value")
            try:
                omi = int(raw) / 1e18
            except (TypeError, ValueError):
                omi = 0.0
            d = _pt_date(it.get("timestamp"))
            if d:
                cur = split.get(d, [0, 0.0, 0.0, 0, 0.0])
                split[d] = [cur[0], cur[1], cur[2], cur[3] + 1, cur[4] + omi]
                n += 1
    return n, newest, stop


def run_gems(state, split, budget_s):
    """Walk resumable des burns gems : backfill (jusqu'au 19/11/2025, la ou
    la pagination s'epuise — quelques dizaines de pages) puis incremental
    via gem_newest_block. Meme patron que le walk des depots 0x821c."""
    t0 = time.time()
    incremental = bool(state.get("gem_done"))
    stop_block = state.get("gem_newest_block") or 0 if incremental else 0
    if incremental:
        print(f"GEMS incremental depuis bloc {stop_block}...", flush=True)
        params = {"type": "ERC-20", "filter": "to"}
    else:
        print(f"GEMS (backfill resumable) : pages deja faites="
              f"{state.get('gem_pages', 0)}...", flush=True)
        params = ({"type": "ERC-20", "filter": "to",
                   **state["gem_next_page"]}
                  if state.get("gem_next_page")
                  else {"type": "ERC-20", "filter": "to"})
    max_pages = int(os.environ.get("BURNS_GEM_MAX_PAGES", "0"))
    pages, buys, run_newest, seen_top = 0, 0, 0, False
    while True:
        if not incremental and time.time() - t0 > budget_s:
            state["gem_next_page"] = params
            print("    gems : budget temps atteint.", flush=True)
            break
        if max_pages and pages >= max_pages:
            if not incremental:
                state["gem_next_page"] = params
            break
        data = _get(GEM_URL, params)
        items = data.get("items") or []
        if not items:
            if not incremental:
                state["gem_done"] = True
                state["gem_next_page"] = None
                print("    GEMS : backfill TERMINE.", flush=True)
            break
        n, newest, stop = _gem_accumulate(split, items,
                                          stop_block if incremental else 0)
        buys += n
        if newest and not seen_top:
            run_newest = max(run_newest, newest)
            seen_top = True
            if not incremental and not state.get("gem_newest_block"):
                state["gem_newest_block"] = newest   # point de reprise fige
        pages += 1
        nxt = data.get("next_page_params")
        if not incremental:
            state["gem_pages"] = int(state.get("gem_pages", 0)) + 1
            if pages % CHECKPOINT_PAGES == 0:
                state["gem_next_page"] = nxt
                _save_split(split)
                _save_state(state)
                print(f"    gems checkpoint : {state['gem_pages']} pages "
                      f"cumulees, {buys} conversions ce run.", flush=True)
        if stop or not nxt:
            if not incremental and not nxt:
                state["gem_done"] = True
                state["gem_next_page"] = None
                print("    GEMS : fin de pagination — backfill TERMINE.",
                      flush=True)
            break
        params = {"type": "ERC-20", "filter": "to", **nxt}
        pause = float(os.environ.get("BURNS_PAUSE", "0"))
        if pause:
            time.sleep(pause)
    if incremental and run_newest:
        state["gem_newest_block"] = run_newest
    return pages, buys


# ---------------------------------------------------------------------------
# Google Sheet (🔥H-BURNS) — optionnel, tolerant, NOMBRES NATIFS + RAW
# ---------------------------------------------------------------------------

def _clean_env(name: str) -> str:
    """Secrets parfois pollues (BOM, guillemets, espaces) — nettoyage doux."""
    v = os.environ.get(name) or ""
    return v.strip().lstrip("﻿").strip('"').strip("'").strip()


def build_sheet_grid(daily, split=None):
    """Grille 🔥H-BURNS : entete + 1 ligne/(jour,source) avec cumul par source
    + decompo NFT/GEM quand le jour est couvert. Les montants sont des
    FLOATS/INTS natifs (jamais des chaines) — ecrits en RAW ils ne passent
    pas par l'interpretation locale FR (fix x100)."""
    split = split or {}
    cumul = defaultdict(float)
    grid = [list(SHEET_HEADER)]
    for (d, src) in sorted(daily):
        tx, omi = daily[(d, src)]
        cumul[src] += omi
        row = [d, src, int(tx), round(omi, 2), round(cumul[src], 2)]
        sp = split.get(d)
        if sp:
            n, omi_n, vol, g, omi_g = sp
            row += [int(n), round(omi_n, 2), int(g), round(omi_g, 2),
                    round(vol, 2)]
        else:
            row += ["", "", "", "", ""]
        grid.append(row)
    return grid


def write_sheet(daily, split=None) -> str:
    raw = _clean_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = _clean_env("SHEET_ID")
    if not raw or not sheet_id:
        return "secrets absents — CSV seul (normal en rodage)."
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(raw),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        sh = gspread.authorize(creds).open_by_key(sheet_id)
        try:
            ws = sh.worksheet(BURNS_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=BURNS_TAB, rows=1000,
                                  cols=len(SHEET_HEADER))
        grid = build_sheet_grid(daily, split)
        ws.clear()
        ws.update(range_name="A1", values=grid, value_input_option="RAW")
        try:
            ws.freeze(rows=1)
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.format(f"D2:E{len(grid)}",
                      {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
            ws.format(f"F2:J{len(grid)}",
                      {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}})
        except Exception:
            pass
        return f"{BURNS_TAB} mis a jour ({len(grid) - 1} lignes)."
    except Exception as e:
        diag = f"len(json)={len(raw)}, 1er car={raw[:1]!r}" if raw else "json vide"
        return f"echec ({e}) [{diag}] — le CSV fait foi."


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

    # gems (v7) AVANT la decompo : walk court (~70 pages au 1er run puis
    # 1-2 pages/jour) — on lui reserve une part du budget restant.
    split = _load_split()
    try:
        gem_pages, gem_buys = run_gems(
            state, split, max(60.0, budget_s - (time.time() - t0)))
        gem_days = sum(1 for v in split.values() if v[3])
        print(f"Gems : {gem_pages} pages / {gem_buys} conversions ce run, "
              f"{gem_days} jours avec burn gems, gem_done="
              f"{state.get('gem_done', False)}.", flush=True)
    except Exception as e:
        print(f"gems warning: {e}", flush=True)
    _save_split(split)

    # decompo NFT avec le budget restant
    try:
        sp_pages, sp_txs = run_split(state, split,
                                     max(60.0, budget_s - (time.time() - t0)))
        print(f"Decompo : {sp_pages} pages / {sp_txs} settles ce run, "
              f"{len(split)} jours couverts, split_done="
              f"{state.get('split_done', False)}.", flush=True)
    except Exception as e:
        print(f"decompo warning: {e}", flush=True)
    _save_split(split)

    _save_daily(daily)
    _save_state(state)
    print("Sheet:", write_sheet(daily, split), flush=True)

    stackr = {k[0]: v for k, v in daily.items() if k[1] == "StackR"}
    total = sum(v[1] for v in stackr.values())
    total_gem = sum(v[4] for v in split.values())
    print(f"\n=== RECAP StackR : {len(stackr)} jours, cumul {total:,.0f} OMI "
          f"(marche) + {total_gem:,.0f} OMI (gems). "
          f"backfill_done={state.get('backfill_done', False)} "
          f"split_done={state.get('split_done', False)} "
          f"gem_done={state.get('gem_done', False)} "
          f"({pages} pages / {deposits} depots ce run, {time.time()-t0:.0f}s)",
          flush=True)
    for d in sorted(stackr)[-8:]:
        tx, omi = stackr[d]
        sp = split.get(d)
        extra = (f" | NFT {sp[1]:>10,.0f} (vol {sp[2]:>12,.0f}) "
                 f"| GEM {sp[4]:>10,.0f}") if sp else ""
        print(f"   {d} | {tx:>4} tx | {omi:>15,.0f} OMI{extra}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
