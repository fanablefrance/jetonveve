"""price_history — le MAGASIN DE PRIX IMPÉRISSABLE de VeVe (jetonveve).

But (demande Preda, 17/07/2026)
-------------------------------
Les 38,5 M de transferts (`transfers-full`) sont AVEUGLES aux prix : la chaine
n'enregistre jamais le floor ni le prix reel. Ce module archive, de facon
PERMANENTE et queryable par URL (parquet), la courbe floor + nb d'offres de
CHAQUE item VeVe depuis sa publication — le socle du tracker (futur payant) et
de meilleures references pour les alertes.

Source
------
`GET my-nft-tracker-backend.azurewebsites.net/api/NftPriceMetrics`
    ?guid=<veve_uuid>&fromTimeStamp=<ISO Z>&toTimeStamp=<ISO|vide>
PUBLIC, sans auth. Renvoie un tableau chronologique de
    {nftUuid, createdTimestamp, lowestMarketPrice(=floor), totalMarketListings}

⚠️ DEUX types de lignes (piege verifie le 17/07) :
  - VRAIE observation : nftUuid == le vrai uuid (heure de crawl irreguliere).
  - REMPLISSAGE carry-forward : nftUuid == 00000000-0000-0000-0000-000000000000,
    a l'heure ronde, prix recopie — padding pour tracer une courbe plate, AUCUNE
    info. => on ne garde QUE nftUuid == uuid, sinon ~24 lignes/jour/item pour rien.
⚠️ Une fenetre trop large renvoie vide/erreur (cap de taille) => on FENETRE les
   appels (PH_WINDOW_DAYS, defaut 120 j) et on decoupe en cas d'echec.

Stockage (compact, fidele)
--------------------------
CSV `veve_uuid,ts_utc,floor,listings`, APPEND-ON-CHANGE : une ligne seulement
quand (floor, listings) change. Une semaine plate = 1 ligne. Le parquet final,
trie par (veve_uuid, ts_utc) et DISTINCT, est queryable par URL (DuckDB httpfs).

Trois modes (env PH_MODE)
-------------------------
  backfill : rejoue tout l'historique via NftPriceMetrics, item par item,
             fenetre par fenetre. REPRENABLE (data/prices/ph_done.txt).
  append   : quotidien, GRATUIT — lit catalogue.csv.gz (floor+listings du jour,
             deja publie) et ajoute les points on-change. Zero appel API.
  publish  : reconstruit prices.parquet + prices.csv.gz (DISTINCT, trie) depuis
             le CSV de travail — auto-guerison des doublons d'un run repris.

Regle des collecteurs longs (lecon du 12/07)
-------------------------------------------
Flush incremental (PH_FLUSH_ITEMS), etat de reprise commite, backoff genereux,
degradation de la fenetre avant abandon, et la RECOLTE EST SACREE : un item qui
echoue est saute (jamais de raise qui jette le reste), retente au run suivant.

Env : PH_MODE, CATALOGUE (catalogue.csv.gz), STORE (prices.csv),
      STATE_DIR (data/prices), PARQUET_OUT (prices.parquet),
      PH_GENESIS (2021-06-01), PH_WINDOW_DAYS (120), PH_WORKERS (4),
      PH_FLUSH_ITEMS (300), PH_MAX_ITEMS (0=tout).
"""

from __future__ import annotations

import csv
import datetime as dt
import glob
import gzip
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

try:
    import requests
except Exception:                      # requests absent en test unitaire pur
    requests = None                    # type: ignore

API = "https://my-nft-tracker-backend.azurewebsites.net/api/NftPriceMetrics"
FILLER_UUID = "00000000-0000-0000-0000-000000000000"
STORE_HEADER = ("veve_uuid", "ts_utc", "floor", "listings")

GENESIS = os.environ.get("PH_GENESIS", "2021-06-01")
WINDOW_DAYS = int(os.environ.get("PH_WINDOW_DAYS", "120"))
MIN_WINDOW_DAYS = int(os.environ.get("PH_MIN_WINDOW_DAYS", "15"))
WORKERS = int(os.environ.get("PH_WORKERS", "4"))
FLUSH_ITEMS = int(os.environ.get("PH_FLUSH_ITEMS", "300"))
MAX_ITEMS = int(os.environ.get("PH_MAX_ITEMS", "0"))
TIMEOUT = int(os.environ.get("PH_TIMEOUT", "40"))
RETRIES = int(os.environ.get("PH_RETRIES", "6"))
BACKOFF = float(os.environ.get("PH_BACKOFF", "2"))
PAUSE = float(os.environ.get("PH_PAUSE", "0.05"))

HEADERS = {
    "accept": "*/*",
    "origin": "https://my-nft-tracker.com",
    "referer": "https://my-nft-tracker.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


# ---------------------------------------------------------------------------
# HTTP (injectable pour les tests)
# ---------------------------------------------------------------------------

def _raw_get(url: str) -> Tuple[Optional[list], str]:
    """GET JSON. Renvoie (data|None, erreur). Ne leve JAMAIS."""
    if requests is None:
        return None, "requests indisponible"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception as e:
                return None, f"json invalide: {str(e)[:80]}"
            return body if isinstance(body, list) else [], ""
        return None, f"HTTP {r.status_code}"
    except Exception as e:                              # reseau : retente
        return None, str(e)[:140]


# Les tests remplacent HTTP_GET par un faux serveur.
HTTP_GET: Callable[[str], Tuple[Optional[list], str]] = _raw_get


# ---------------------------------------------------------------------------
# Utilitaires valeurs
# ---------------------------------------------------------------------------

def _num(x) -> Optional[float]:
    """Convertit en float pour COMPARER (idempotent : '134' == 134.0)."""
    if x in (None, ""):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x) -> Optional[int]:
    v = _num(x)
    return int(v) if v is not None else None


def _key(floor, listings) -> Tuple[Optional[float], Optional[int]]:
    return (_num(floor), _int(listings))


def _iso(d: dt.date) -> str:
    return d.strftime("%Y-%m-%dT00:00:00.000Z")


def _parse_release_date(s: str) -> Optional[dt.date]:
    """catalogue.release_date = 'DD/MM/YYYY HH:MM:SS' (ou vide)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Récupération d'un item (fenêtrée, dégradante, reprenable)
# ---------------------------------------------------------------------------

def fetch_window(uuid: str, frm: dt.date, to: dt.date) -> Tuple[Optional[list], str]:
    """Une fenetre [frm, to). Renvoie (vraies_obs|None, err).

    Une reponse HTTP 200 VIDE est valide (aucune offre sur la periode) -> []. Un
    None signale un echec reseau/HTTP a retenter/decouper. Ne leve jamais.
    """
    url = f"{API}?guid={uuid}&fromTimeStamp={_iso(frm)}&toTimeStamp={_iso(to)}"
    last = ""
    for attempt in range(1, RETRIES + 1):
        data, err = HTTP_GET(url)
        if data is not None:
            return [d for d in data if d.get("nftUuid") == uuid], ""
        last = err
        time.sleep(BACKOFF * attempt)
    return None, last


def fetch_range(uuid: str, frm: dt.date, to: dt.date) -> Tuple[list, bool]:
    """[frm, to) en decoupant si une fenetre resiste. Renvoie (obs, ok).

    ok=False => au moins un sous-bloc n'a jamais repondu (item saute, retente
    au prochain run). La recolte des blocs OK est CONSERVEE.
    """
    rows, _ = fetch_window(uuid, frm, to)
    if rows is not None:
        return rows, True
    span = (to - frm).days
    if span <= MIN_WINDOW_DAYS:
        return [], False
    mid = frm + dt.timedelta(days=span // 2)
    left, okl = fetch_range(uuid, frm, mid)
    right, okr = fetch_range(uuid, mid, to)
    return left + right, (okl and okr)


def backfill_item(uuid: str, start: dt.date, end: dt.date) -> Tuple[List[tuple], bool]:
    """Rejoue l'historique complet d'un item. Renvoie (lignes_on_change, ok)."""
    obs: list = []
    ok = True
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=WINDOW_DAYS), end)
        part, okp = fetch_range(uuid, cur, nxt)
        obs.extend(part)
        ok = ok and okp
        time.sleep(PAUSE)
        cur = nxt
    return compress(uuid, obs), ok


# ---------------------------------------------------------------------------
# Compression on-change
# ---------------------------------------------------------------------------

def compress(uuid: str, rows: list, seed: Tuple = (object(),)) -> List[tuple]:
    """rows = obs brutes (dicts). Emet (uuid, ts, floor, listings) quand (floor,
    listings) change. `seed` = derniere valeur connue (mode append) pour ne pas
    reemettre un point identique au dernier deja stocke.
    """
    out: List[tuple] = []
    last = seed
    for d in sorted(rows, key=lambda x: str(x.get("createdTimestamp", ""))):
        floor = d.get("lowestMarketPrice")
        listings = d.get("totalMarketListings")
        k = _key(floor, listings)
        if k == (None, None):
            continue
        if k != last:
            out.append((uuid, d.get("createdTimestamp"), floor, listings))
            last = k
    return out


# ---------------------------------------------------------------------------
# Catalogue & état
# ---------------------------------------------------------------------------

def _open_maybe_gz(path: str):
    return (gzip.open(path, "rt", encoding="utf-8", newline="")
            if path.endswith(".gz")
            else open(path, encoding="utf-8", newline=""))


def read_catalogue(path: str) -> List[dict]:
    """Renvoie [{uuid, release_date, floor, listings}, ...] (uuid non vide)."""
    with _open_maybe_gz(path) as f:
        out = []
        for row in csv.DictReader(f):
            uuid = (row.get("uuid") or "").strip()
            if uuid:
                out.append(row)
        return out


def load_done(state_dir: str) -> set:
    p = os.path.join(state_dir, "ph_done.txt")
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return {ln.strip() for ln in f if ln.strip()}


def save_done(state_dir: str, done: set) -> None:
    os.makedirs(state_dir, exist_ok=True)
    p = os.path.join(state_dir, "ph_done.txt")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(done)))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Écriture du store
# ---------------------------------------------------------------------------

def _ensure_store(store: str) -> None:
    if not os.path.exists(store) or os.path.getsize(store) == 0:
        os.makedirs(os.path.dirname(store) or ".", exist_ok=True)
        with open(store, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(STORE_HEADER)


def append_rows(store: str, rows: List[tuple]) -> int:
    if not rows:
        return 0
    _ensure_store(store)
    with open(store, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow(r)
    return len(rows)


def last_values(source: str) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """Derniere (floor, listings) par uuid, depuis le parquet OU le csv de travail.
    Sert au mode append (ne pas reemettre un point identique au dernier stocke).
    """
    last: Dict[str, Tuple] = {}
    if not source or not os.path.exists(source):
        return last
    try:
        import duckdb
        rel = ("read_parquet('%s')" % source if source.endswith(".parquet")
               else "read_csv_auto('%s', header=true)" % source)
        q = (f"SELECT veve_uuid, arg_max(floor, ts_utc) f, "
             f"arg_max(listings, ts_utc) l FROM {rel} GROUP BY veve_uuid")
        for uuid, f, l in duckdb.connect().execute(q).fetchall():
            last[str(uuid)] = _key(f, l)
        return last
    except Exception:
        pass
    # Repli sans duckdb : dernier vu dans le fichier (ordre d'append ~ chrono).
    try:
        with _open_maybe_gz(source) as fh:
            for row in csv.DictReader(fh):
                last[row["veve_uuid"]] = _key(row.get("floor"), row.get("listings"))
    except Exception:
        return {}
    return last


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_backfill(catalogue: str, store: str, state_dir: str) -> int:
    cat = read_catalogue(catalogue)
    done = load_done(state_dir)
    genesis = _parse_release_date(GENESIS) or dt.date(2021, 6, 1)
    end = dt.datetime.utcnow().date() + dt.timedelta(days=1)

    todo = [r for r in cat if r["uuid"].strip() not in done]
    if MAX_ITEMS:
        todo = todo[:MAX_ITEMS]
    print(f"backfill : {len(cat)} items catalogue, {len(done)} deja faits, "
          f"{len(todo)} a traiter (fenetre {WINDOW_DAYS} j, {WORKERS} workers).",
          flush=True)
    if not todo:
        print("Rien a faire — backfill deja complet.", flush=True)
        return 0

    def work(row: dict) -> Tuple[str, List[tuple], bool]:
        uuid = row["uuid"].strip()
        start = _parse_release_date(row.get("release_date", "")) or genesis
        if start < genesis:
            start = genesis
        lines, ok = backfill_item(uuid, start, end)
        return uuid, lines, ok

    total_rows = failed = processed = 0
    for i in range(0, len(todo), FLUSH_ITEMS):
        chunk = todo[i:i + FLUSH_ITEMS]
        buf: List[tuple] = []
        ok_uuids: List[str] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for uuid, lines, ok in ex.map(work, chunk):
                processed += 1
                if ok:
                    buf.extend(lines)
                    ok_uuids.append(uuid)          # RECOLTE SACREE : on ne marque
                else:                              # done QUE si l'item est complet
                    failed += 1
        # flush : ecrire AVANT de marquer done (une coupure => re-fetch propre,
        # les doublons eventuels sont fondus par le DISTINCT du mode publish).
        total_rows += append_rows(store, buf)
        done.update(ok_uuids)
        save_done(state_dir, done)
        print(f"  lot {i // FLUSH_ITEMS + 1} : {processed}/{len(todo)} items, "
              f"+{len(buf)} lignes (total {total_rows}), {failed} sautes.",
              flush=True)

    tag = "INCOMPLET" if failed else "COMPLET"
    print(f"{tag} : {processed} items, {total_rows} lignes on-change ecrites, "
          f"{failed} sautes (retentes au prochain run).", flush=True)
    return 0


def run_append(catalogue: str, store: str, source_for_last: str) -> int:
    cat = read_catalogue(catalogue)
    last = last_values(source_for_last)
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    buf: List[tuple] = []
    for row in cat:
        uuid = row["uuid"].strip()
        floor, listings = row.get("floor"), row.get("listings")
        k = _key(floor, listings)
        if k == (None, None):                      # colonne vide = INCONNU
            continue
        if last.get(uuid) != k:                    # on-change vs dernier stocke
            buf.append((uuid, now, floor, listings))
    n = append_rows(store, buf)
    print(f"append : {len(cat)} items catalogue, {n} points on-change ajoutes "
          f"(date {now}).", flush=True)
    return 0


def run_baselines(source: str, out_csv: str, cap: float = 1e9) -> int:
    """Derive un fichier COMPACT de references par item (percentiles floor +
    baseline offres). 1 ligne / uuid. Consomme par les alertes."""
    if not source or not os.path.exists(source):
        print("baselines : aucune source — rien a deriver.", flush=True)
        return 0
    import duckdb
    rel = (f"read_parquet('{source}')" if source.endswith(".parquet")
           else f"read_csv_auto('{source}', header=true)")
    con = duckdb.connect()
    q = f"""
        COPY (
          WITH src AS (
            SELECT veve_uuid, CAST(floor AS DOUBLE) AS floor,
                   CAST(listings AS DOUBLE) AS listings, ts_utc
            FROM {rel}
            WHERE TRY_CAST(floor AS DOUBLE) > 0
              AND TRY_CAST(floor AS DOUBLE) < {cap}
          )
          SELECT veve_uuid, count(*) AS n_points,
                 min(ts_utc) AS first_ts, max(ts_utc) AS last_ts,
                 min(floor) AS floor_min,
                 quantile_cont(floor, 0.05) AS floor_p5,
                 quantile_cont(floor, 0.25) AS floor_p25,
                 quantile_cont(floor, 0.50) AS floor_p50,
                 quantile_cont(floor, 0.75) AS floor_p75,
                 quantile_cont(floor, 0.95) AS floor_p95,
                 max(floor) AS floor_max,
                 quantile_cont(listings, 0.50) AS listings_p50,
                 quantile_cont(listings, 0.90) AS listings_p90,
                 max(listings) AS listings_max,
                 arg_max(floor, ts_utc) AS last_floor,
                 arg_max(listings, ts_utc) AS last_listings
          FROM src GROUP BY veve_uuid ORDER BY veve_uuid
        ) TO '{out_csv}' (FORMAT csv, HEADER, COMPRESSION gzip)
    """
    con.execute(q)
    n = con.execute(
        f"SELECT count(*) FROM read_csv_auto('{out_csv}', header=true)").fetchone()[0]
    print(f"baselines : {n} items references -> {out_csv}.", flush=True)
    return 0


def run_publish(store: str, parquet_out: str, csv_out: str) -> int:
    """Reconstruit parquet + csv.gz DISTINCT et tries. Auto-guerit les doublons."""
    if not os.path.exists(store):
        print("publish : aucun store a publier.", flush=True)
        return 0
    import duckdb
    con = duckdb.connect()
    src = f"read_csv_auto('{store}', header=true)"
    n_in = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    dedup = (f"SELECT DISTINCT veve_uuid, ts_utc, floor, listings FROM {src} "
             f"ORDER BY veve_uuid, ts_utc")
    con.execute(f"COPY ({dedup}) TO '{parquet_out}' (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"COPY ({dedup}) TO '{csv_out}' (FORMAT csv, HEADER, COMPRESSION gzip)")
    n_out = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_out}')").fetchone()[0]
    n_items = con.execute(
        f"SELECT count(DISTINCT veve_uuid) FROM read_parquet('{parquet_out}')"
    ).fetchone()[0]
    print(f"publish : {n_in} lignes brutes -> {n_out} distinctes sur {n_items} "
          f"items. Ecrits : {parquet_out} + {csv_out}.", flush=True)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    mode = os.environ.get("PH_MODE", "append").strip().lower()
    catalogue = os.environ.get("CATALOGUE", "catalogue.csv.gz")
    store = os.environ.get("STORE", "prices.csv")
    state_dir = os.environ.get("STATE_DIR", os.path.join("data", "prices"))
    parquet_out = os.environ.get("PARQUET_OUT", "prices.parquet")
    csv_out = os.environ.get("CSV_OUT", "prices.csv.gz")
    os.makedirs(state_dir, exist_ok=True)

    if mode == "backfill":
        return run_backfill(catalogue, store, state_dir)
    if mode == "append":
        # reference = parquet publie s'il existe, sinon le csv de travail
        src = parquet_out if os.path.exists(parquet_out) else store
        return run_append(catalogue, store, src)
    if mode == "publish":
        return run_publish(store, parquet_out, csv_out)
    if mode == "baselines":
        out = os.environ.get("BASELINES_OUT", "prices_baselines.csv.gz")
        src = parquet_out if os.path.exists(parquet_out) else store
        return run_baselines(src, out)
    print(f"PH_MODE inconnu : {mode!r} (backfill|append|publish|baselines)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
