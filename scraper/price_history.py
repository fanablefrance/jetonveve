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
    ?guid=<id my-nft-tracker>&fromTimeStamp=<ISO Z>&toTimeStamp=<ISO|vide>
⚠️ le `guid` est l'id INTERNE my-nft-tracker (champ `uuid` de /api/Nfts),
PAS le veve_uuid (= externalReference, celui du catalogue). D'ou la carte
veve_uuid->id tracker (build_id_map). Les COMICS n'ont pas de courbe ici
(NftPriceMetrics renvoie [] seulement pour les items JAMAIS trades) -> on
backfille tout ; les items sans historique sont clos a 0 ligne, gratuitement.
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
      PH_FLUSH_ITEMS (300), PH_MAX_ITEMS (0=tout), PH_MAX_MINUTES (300 ; budget
      temps -> arret propre avant timeout, 0=illimite).
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
API_NFTS = "https://my-nft-tracker-backend.azurewebsites.net/api/Nfts"
NFTS_PAGE = int(os.environ.get("PH_NFTS_PAGE", "24"))   # >24 corrompt la pagination
# NftPriceMetrics sert la courbe des COLLECTIBLES ET des comics TRADES (confirme
# 17/07 : un comic grail a un historique complet ; seuls les comics jamais trades
# renvoient [] -> 0 ligne, clos proprement). Defaut vide = TOUT le catalogue ;
# PH_BACKFILL_KINDS=collectible pour ne faire que les collectibles.
BACKFILL_KINDS = {k.strip().lower() for k in
                  os.environ.get("PH_BACKFILL_KINDS", "").split(",")
                  if k.strip()}

GENESIS = os.environ.get("PH_GENESIS", "2021-06-01")
WINDOW_DAYS = int(os.environ.get("PH_WINDOW_DAYS", "120"))
MIN_WINDOW_DAYS = int(os.environ.get("PH_MIN_WINDOW_DAYS", "15"))
WORKERS = int(os.environ.get("PH_WORKERS", "4"))
FLUSH_ITEMS = int(os.environ.get("PH_FLUSH_ITEMS", "300"))
MAX_ITEMS = int(os.environ.get("PH_MAX_ITEMS", "0"))
# ⏱️ BUDGET DE TEMPS (min) : le backfill s'arrete PROPREMENT avant le timeout du
# job GitHub (350 min) pour laisser les etapes publish+commit persister la
# progression -> `max_items=0` devient sur, chaque run sauve sa recolte. 0 = illimite.
MAX_MINUTES = float(os.environ.get("PH_MAX_MINUTES", "300"))
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
            return body, ""
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
        if isinstance(data, list):
            return [d for d in data if d.get("nftUuid") == uuid], ""
        last = err or "reponse inattendue (non-liste)"
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


def backfill_item(veve_uuid: str, tracker_id: str, start: dt.date,
                  end: dt.date) -> Tuple[List[tuple], bool]:
    """Rejoue l'historique complet d'un item. `tracker_id` = guid my-nft-tracker
    (!= veve_uuid) interroge a l'API ; les lignes sont stockees sous veve_uuid."""
    obs: list = []
    ok = True
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=WINDOW_DAYS), end)
        part, okp = fetch_range(tracker_id, cur, nxt)
        obs.extend(part)
        ok = ok and okp
        time.sleep(PAUSE)
        cur = nxt
    return compress(veve_uuid, obs), ok


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
               else f"read_csv_auto('{source}', header=true)")
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

def fetch_nfts_page(offset: int) -> Tuple[Optional[list], Optional[int], str]:
    """Une page /api/Nfts. Renvoie (records|None, total|None, err). Ne leve jamais."""
    url = f"{API_NFTS}?offset={offset}&limit={NFTS_PAGE}"
    last = ""
    for attempt in range(1, RETRIES + 1):
        data, err = HTTP_GET(url)
        if isinstance(data, dict):
            recs = data.get("resultEntries") or []
            tot = (data.get("meta") or {}).get("entries_TotalAvailable")
            return recs, (int(tot) if tot is not None else None), ""
        last = err or "reponse inattendue"
        time.sleep(BACKOFF * attempt)
    return None, None, last


def _id_map_from_catalogue(cat: List[dict]) -> Dict[str, str]:
    """Si le catalogue porte une colonne `tracker_id` (fix propre cote preda),
    on l'utilise -> aucun balayage /api/Nfts. Sinon {} (on balaiera)."""
    m: Dict[str, str] = {}
    for r in cat:
        tid = (r.get("tracker_id") or "").strip()
        uid = (r.get("uuid") or "").strip()
        if tid and uid:
            m[uid] = tid
    return m


def build_id_map(state_dir: str, refresh: bool = False) -> Tuple[Dict[str, str], bool]:
    """Map veve_uuid (externalReference) -> id my-nft-tracker (uuid = le guid des
    metriques !). Balayage /api/Nfts, MIS EN CACHE (data/prices/id_map.json) et
    repris (PH_MAP_FLUSH pages). Renvoie (map, complet). Ne perd jamais sa recolte.
    """
    path = os.path.join(state_dir, "id_map.json")
    id_map: Dict[str, str] = {}
    offset = 0
    if os.path.exists(path) and not refresh:
        try:
            saved = json.load(open(path, encoding="utf-8"))
            id_map = dict(saved.get("map", {}))
            offset = int(saved.get("offset", 0))
            if saved.get("done"):
                print(f"id_map : {len(id_map)} items (cache complet).", flush=True)
                return id_map, True
        except Exception:
            id_map, offset = {}, 0

    def _save(off: int, done: bool):
        os.makedirs(state_dir, exist_ok=True)
        tmp = path + ".tmp"
        json.dump({"map": id_map, "offset": off, "done": done},
                  open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, path)

    flush = int(os.environ.get("PH_MAP_FLUSH", "50"))
    total = None
    page_no = 0
    while True:
        recs, tot, err = fetch_nfts_page(offset)
        if recs is None:
            print(f"  id_map : offset={offset} en echec ({err}) — arret, "
                  f"{len(id_map)} mappings gardes (repris au prochain run).",
                  flush=True)
            _save(offset, False)
            return id_map, False
        if tot is not None:
            total = tot
        for r in recs:
            ext = (r.get("externalReference") or "").strip()
            tid = (r.get("uuid") or "").strip()
            if ext and tid:
                id_map[ext] = tid
        offset += NFTS_PAGE
        page_no += 1
        if page_no % flush == 0:
            _save(offset, False)
            print(f"  id_map : {offset} balayes, {len(id_map)} mappings.", flush=True)
        if not recs or (total is not None and offset >= total):
            break
    _save(offset, True)
    print(f"id_map : balayage complet, {len(id_map)} mappings.", flush=True)
    return id_map, True


def _write_status(txt: str) -> None:
    """Ecrit COMPLET / <nb items faits ce run> pour que le workflow decide de
    s'auto-enchainer (backfill non fini) ou de s'arreter."""
    try:
        with open(os.environ.get("PH_STATUS_FILE", "ph_status.txt"), "w",
                  encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass


def run_backfill(catalogue: str, store: str, state_dir: str) -> int:
    t0 = time.time()
    cat = read_catalogue(catalogue)
    done = load_done(state_dir)
    n_done0 = len(done)
    genesis = _parse_release_date(GENESIS) or dt.date(2021, 6, 1)
    end = dt.datetime.utcnow().date() + dt.timedelta(days=1)

    # Le guid des metriques = l'id INTERNE my-nft-tracker (!= veve_uuid). Carte
    # depuis le catalogue si dispo, sinon balayage /api/Nfts (cache, repris).
    id_map = _id_map_from_catalogue(cat)
    map_complete = bool(id_map)
    if not id_map:
        id_map, map_complete = build_id_map(
            state_dir, refresh=os.environ.get("PH_REFRESH_MAP") == "1")

    todo = [r for r in cat if r["uuid"].strip() not in done]
    if MAX_ITEMS:
        todo = todo[:MAX_ITEMS]
    print(f"backfill : {len(cat)} items catalogue, {len(done)} deja faits, "
          f"{len(todo)} a traiter (fenetre {WINDOW_DAYS} j, {WORKERS} workers).",
          flush=True)
    if not todo:
        print("Rien a faire — backfill deja complet.", flush=True)
        return 0

    def work(row: dict):
        veve = row["uuid"].strip()
        kind = (row.get("kind") or "").strip().lower()
        if BACKFILL_KINDS and kind and kind not in BACKFILL_KINDS:
            return veve, [], "done0"     # ex. comics : pas de time-series tracker
        tid = id_map.get(veve)
        if not tid:
            # pas d'id tracker : si la carte est COMPLETE, l'item n'a pas de fiche
            # tracker -> rien a collecter, on le clot ; sinon on le reprend plus tard.
            return veve, [], ("done0" if map_complete else "noid")
        start = _parse_release_date(row.get("release_date", "")) or genesis
        if start < genesis:
            start = genesis
        lines, ok = backfill_item(veve, tid, start, end)
        return veve, lines, ("ok" if ok else "fail")

    total_rows = failed = noid = processed = 0
    stopped_early = False
    for i in range(0, len(todo), FLUSH_ITEMS):
        if MAX_MINUTES and (time.time() - t0) > MAX_MINUTES * 60:
            stopped_early = True
            print(f"  ⏱️ budget de temps atteint ({MAX_MINUTES:g} min) — arret PROPRE "
                  f"a {processed}/{len(todo)} items ; publish+commit vont persister, "
                  f"le prochain run reprend.", flush=True)
            break
        chunk = todo[i:i + FLUSH_ITEMS]
        buf: List[tuple] = []
        ok_uuids: List[str] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for veve, lines, status in ex.map(work, chunk):
                processed += 1
                if status in ("ok", "done0"):
                    buf.extend(lines)
                    ok_uuids.append(veve)          # RECOLTE SACREE : done seulement
                elif status == "fail":             # si complet (ou sans fiche)
                    failed += 1
                else:                              # noid : carte incomplete -> retente
                    noid += 1
        total_rows += append_rows(store, buf)
        done.update(ok_uuids)
        save_done(state_dir, done)
        print(f"  lot {i // FLUSH_ITEMS + 1} : {processed}/{len(todo)} items, "
              f"+{len(buf)} lignes (total {total_rows}), {failed} sautes, "
              f"{noid} sans id.", flush=True)

    tag = "INCOMPLET" if (failed or noid or stopped_early) else "COMPLET"
    print(f"{tag} : {processed} items, {total_rows} lignes on-change ecrites, "
          f"{failed} sautes + {noid} sans id"
          + (" + budget temps atteint" if stopped_early else "")
          + " (retentes au prochain run).", flush=True)
    _write_status("COMPLET" if tag == "COMPLET" else str(len(done) - n_done0))
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
    """Reconstruit parquet + csv.gz DISTINCT et tries. Auto-guerit les doublons.

    ⚠️ On lit le store en PYTHON (module csv), PAS via le sniffer DuckDB : ce
    dernier a plante en prod (« could not detect dialect ») des qu'une ligne a un
    nombre de colonnes inattendu. csv.reader gere le quoting et on saute proprement
    les lignes malformees (!= 4 champs). Puis DuckDB ecrit le parquet depuis un CSV
    PROPRE (colonnes garanties)."""
    if not os.path.exists(store):
        print("publish : aucun store a publier.", flush=True)
        return 0
    import csv as _csv
    import duckdb
    seen = {}
    n_in = bad = 0
    with open(store, encoding="utf-8", newline="") as f:
        rd = _csv.reader(f)
        next(rd, None)                                   # entete
        for row in rd:
            n_in += 1
            if len(row) != 4 or not row[0] or not row[1]:
                bad += 1
                continue
            seen[(row[0], row[1], row[2], row[3])] = None   # dedup exact
    rows = sorted(seen.keys(), key=lambda t: (t[0], t[1]))
    clean = store + ".clean"
    with open(clean, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(STORE_HEADER)
        w.writerows(rows)
    con = duckdb.connect()
    src = ("read_csv('%s', header=true, columns={'veve_uuid':'VARCHAR',"
           "'ts_utc':'VARCHAR','floor':'VARCHAR','listings':'VARCHAR'})" % clean)
    q = ("SELECT veve_uuid, ts_utc, TRY_CAST(floor AS DOUBLE) AS floor, "
         "TRY_CAST(listings AS BIGINT) AS listings FROM %s "
         "WHERE TRY_CAST(floor AS DOUBLE) IS NOT NULL "
         "ORDER BY veve_uuid, ts_utc" % src)
    con.execute(f"COPY ({q}) TO '{parquet_out}' (FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"COPY ({q}) TO '{csv_out}' (FORMAT csv, HEADER, COMPRESSION gzip)")
    n_out = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_out}')").fetchone()[0]
    n_items = con.execute(
        f"SELECT count(DISTINCT veve_uuid) FROM read_parquet('{parquet_out}')"
    ).fetchone()[0]
    try:
        os.remove(clean)
    except OSError:
        pass
    print(f"publish : {n_in} lignes lues ({bad} ecartees) -> {n_out} distinctes "
          f"sur {n_items} items. Ecrits : {parquet_out} + {csv_out}.", flush=True)
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
