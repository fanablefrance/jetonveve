#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger_writer — writer LEGER du ledger (preda, 16/07/2026).

Le calcul lourd (rejeu 38 M lignes) vit sur jetonveve (scraper/ledger_derived.py,
Release publique `analytics-derived`). Ici on TELECHARGE les CSV derives et on
ecrit les MEMES onglets Sheet que scraper/ledger.py, dont on IMPORTE les
fonctions d'ecriture (parite garantie au caractere pres) :

  _MonthlyPulse   <- pulse.csv           (22 col, mois + annees)
  _Reveils        <- reveils.csv
  🎯A-CORNERISATION <- corner_full.csv.gz (111 col)
  _WalletSize     <- wallet_size.csv     (upsert du mois)
  _Whales         <- whales.csv          (3 blocs, pseudo joint ici)
  🟣C-PSEUDOS      <- profiles_full.csv.gz (enrichissement profils + rangs)
  data/wallet_profiles.csv.gz commite (drop-in de l'ancien fichier prod).
  data/ledger.csv.gz N'EST PLUS commite : ledger_full.csv.gz vit dans la
  Release analytics-derived (trop gros pour git, ~limite GitHub 100 Mo).

Garde-fous : meta_ledger.csv obligatoire, run_date pas plus vieux que
MAX_AGE_DAYS (defaut 7), en-tete corner == CORNER_HEADER, en-tete pulse ==
PULSE_HEADER, sinon exit 1 SANS toucher au Sheet. RELEASE_BASE surchargeble
(secours N-1 : .../analytics-derived-prev).

Usage : python -m scraper.ledger_writer
Env   : SHEET_ID (requis), RUN_DATE, RUN_STEPS (all|whales,corner,size,
        pseudos,pulse,save), MAX_AGE_DAYS, RELEASE_BASE.
"""
import csv
import datetime as _dt
import gzip
import io
import os
import sys
import time
import urllib.request

from scraper.ledger import (CORNER_HEADER, CORNER_TAB, PULSE_HEADER,
                            RANK_COLS, WHALE_TYPES, _enrich_pseudos,
                            _open_with_retry, _read_pseudos, _save_profiles,
                            _write, _write_pulse, _write_reveils,
                            _write_size_history, _write_whales_flat)
from scraper.sheets import append_log
from scraper import sheet_format as _fmt

BASE = os.environ.get(
    "RELEASE_BASE",
    "https://github.com/fanablefrance/jetonveve/releases/download/analytics-derived")
MAX_AGE = int(os.environ.get("MAX_AGE_DAYS", "7"))
SIZE_HEADER_LOCAL = ["dimension", "bucket", "wallets", "pct_wallets",
                     "total", "pct_total"]
PROFILE_KEYS = ["holdings", "distinct_collectibles", "acquired", "sold",
                "retention", "median_hold_days", "collectorScore",
                "activityStatus", "engagementLevel", "value_store",
                "value_floor", "qty_bucket", "airdropOnly", "last_active",
                "listed"]


def _fetch(name: str) -> bytes:
    url = f"{BASE}/{name}"
    req = urllib.request.Request(url, headers={"User-Agent": "veve-ledger-writer/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    print(f"  {name} : {len(data)/1e6:.1f} Mo", flush=True)
    return data


def _rows(name: str):
    data = _fetch(name)
    if name.endswith(".gz"):
        data = gzip.decompress(data)
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def _num(x: str):
    """Coercition CSV -> types Sheet (les nombres redeviennent des nombres,
    les chaines restent des chaines, '' reste vide)."""
    if x == "":
        return ""
    try:
        return int(x)
    except ValueError:
        pass
    try:
        return float(x)
    except ValueError:
        return x


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERROR: SHEET_ID requis.", file=sys.stderr)
        return 2
    rd = os.environ.get("RUN_DATE")
    today = _dt.date.fromisoformat(rd) if rd else _dt.date.today()
    steps = [s.strip() for s in
             (os.environ.get("RUN_STEPS") or "all").split(",") if s.strip()]

    def do(step: str) -> bool:
        return "all" in steps or step in steps

    # ── garde-fous AVANT d'ouvrir le Sheet ──────────────────────────────────
    print(f"Release : {BASE}", flush=True)
    meta = _rows("meta_ledger.csv")
    meta_d = dict(zip(meta[0], meta[1]))
    run_date = meta_d.get("run_date", "")
    age = (today - _dt.date.fromisoformat(run_date)).days
    print(f"meta : run_date={run_date} (age {age} j), circulant="
          f"{meta_d.get('circulant')}, holders={meta_d.get('holders')}", flush=True)
    if age > MAX_AGE:
        print(f"ERREUR : derives vieux de {age} j > MAX_AGE_DAYS {MAX_AGE} — "
              f"relancer analytics.yml sur jetonveve (ou RELEASE_BASE=...-prev).")
        return 1

    pulse = _rows("pulse.csv")
    if pulse[0] != PULSE_HEADER:
        print(f"ERREUR : en-tete pulse inattendu : {pulse[0]}")
        return 1
    pulse_rows = [[_num(c) for c in r] for r in pulse[1:]]

    reveils_rows = _rows("reveils.csv")[1:]
    reveils = {r[0]: int(r[1]) for r in reveils_rows}

    corner = _rows("corner_full.csv.gz")
    if corner[0] != CORNER_HEADER:
        print(f"ERREUR : en-tete corner inattendu ({len(corner[0])} col vs "
              f"{len(CORNER_HEADER)}).")
        return 1
    hm_idx = {CORNER_HEADER.index(c) for c in ("hm_prof_act", "hm_prof_ws", "hm_act_ws")}
    corner_rows = [[c if i in hm_idx else _num(c) for i, c in enumerate(r)]
                   for r in corner[1:]]

    size = _rows("wallet_size.csv")
    if size[0] != SIZE_HEADER_LOCAL:
        print(f"ERREUR : en-tete wallet_size inattendu : {size[0]}")
        return 1
    size_rows = [[_num(c) for c in r] for r in size[1:]]

    whales = _rows("whales.csv")[1:]          # block,rank,wallet,metric,...
    profs = _rows("profiles_full.csv.gz")
    p_hdr = profs[0]
    if p_hdr != ["wallet"] + PROFILE_KEYS:
        print(f"ERREUR : en-tete profiles inattendu : {p_hdr}")
        return 1

    # ── Sheet ────────────────────────────────────────────────────────────────
    sh = _open_with_retry(sheet_id)
    pseudos = _read_pseudos(sh)

    profiles = {}
    for r in profs[1:]:
        w = r[0]
        pr = {k: _num(v) for k, v in zip(PROFILE_KEYS, r[1:])}
        pr["pseudo"] = pseudos.get(w, "")
        profiles[w] = pr
    whale_blocks = []
    for title, key in WHALE_TYPES:
        rows = []
        for b, rank, w, metric, h, dc, vs, vf, sc, ac in whales:
            if b != title:
                continue
            rows.append([int(rank), w, pseudos.get(w, ""), _num(metric),
                         _num(h), _num(dc), _num(vs), _num(vf), sc, ac])
            if w in profiles:
                profiles[w][RANK_COLS[key]] = int(rank)
        rows.sort(key=lambda x: x[0])
        whale_blocks.append((title, rows))
    print(f"charge : {len(corner_rows)} items corner, {len(pulse_rows)} lignes "
          f"pulse, {len(profiles)} profils, {sum(len(r) for _, r in whale_blocks)} "
          f"whales.", flush=True)

    enriched = 0
    if do("save"):
        _save_profiles(profiles,
                       os.environ.get("PROFILES_OUT", "data/wallet_profiles.csv.gz"))
    if do("whales"):
        _write_whales_flat(sh, whale_blocks)
    if do("corner"):
        _write(sh, CORNER_TAB, CORNER_HEADER, corner_rows)
    if do("size"):
        _write_size_history(sh, size_rows, run_date[:7])
    if do("pseudos"):
        enriched = _enrich_pseudos(sh, profiles)
    if do("pulse"):
        try:
            _write_pulse(sh, pulse_rows)
            _write_reveils(sh, reveils)
        except Exception as e:
            print(f"pulse warning: {e}", flush=True)

    try:
        from scraper.stackr import PSEUDOS_HEADER
        from scraper.ledger import SIZE_TAB, SIZE_HEADER
        _fmt.format_tab(sh, CORNER_TAB, CORNER_HEADER, header_rows=1)
        _fmt.format_tab(sh, SIZE_TAB, SIZE_HEADER, header_rows=1)
        _fmt.format_tab(sh, "🟣C-PSEUDOS", PSEUDOS_HEADER, header_rows=1)
    except Exception as e:
        print(f"formatting warning: {e}", flush=True)

    summary = {"status": "OK", "source_run": run_date,
               "collectibles": len(corner_rows), "holders": len(profiles),
               "pseudos_enriched": enriched,
               "duration": f"{time.time()-t0:.0f}s"}
    try:
        append_log(sheet_id, "ledger-writer", "OK",
                   "; ".join(f"{k}={v}" for k, v in summary.items() if k != "status"))
    except Exception as e:
        print(f"log warning: {e}", flush=True)
    print(f"Done. {summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
