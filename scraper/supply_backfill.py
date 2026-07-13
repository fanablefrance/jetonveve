"""
Backfill des colonnes FROIDES supply / store_price_gems / mcp_priority /
veve_exclusive sur les deux catalogues du Sheet (2026-07-13, chantier
"page Classement des series comics").

POURQUOI CE SCRIPT
------------------
Ces valeurs etaient DEJA collectees par l'enrichissement VeVe GraphQL
(`COMIC_QUERY` demande `totalIssued`, `storePrice`, ...) puis JETEES juste avant
l'ecriture par `sheets.DROP_COLUMNS`. Le patch de `sheets.py` les conserve
desormais pour les NOUVEAUX drops — mais les ~18 700 lignes deja en place
resteraient vides. Ce script les remplit.

CE QU'IL NE FAIT PAS
--------------------
Il ne touche PAS a my-nft-tracker (zero requete catalogue) : il part des uuid
DEJA dans le Sheet. Un `ENRICH_MODE=all` aurait re-scrape les 18 700 fiches du
tracker pour rien — discretion oblige (cf. preferences de Preda).
Il n'ecrit QUE les colonnes ci-dessus, colonne par colonne : aucune autre cellule
du catalogue n'est reecrite, donc rien a perdre en cas d'interruption.

REGLE DES COLLECTEURS LONGS (leçon du 12/07)
-------------------------------------------
- ecriture par LOTS (WRITE_EVERY series enrichies) : un run coupe garde tout ce
  qui a ete recolte avant la coupure ;
- reprise naturelle : au run suivant, les lignes deja remplies sont ignorees
  (on ne redemande a VeVe que ce qui manque) ;
- backoff sur erreur reseau, et une page en echec n'arrete pas le run.

SONDE MCP
---------
Le "5 000 MCP a depenser pour l'acces prioritaire" n'est PAS `dailyMcpPoints`
(=6). L'introspection GraphQL etant desactivee, on teste au demarrage une liste
de noms candidats, un par un, sur un comic connu : VeVe repond HTTP 400
"Cannot query field X" quand le champ n'existe pas. Les champs acceptes sont
ajoutes a la requete et ecrits dans `mcp_priority` ; si aucun ne passe, la
colonne reste MANUELLE (et le script le dit clairement).

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID
      MAX_ITEMS (0 = tout), WORKERS (defaut 6), WRITE_EVERY (defaut 400),
      ONLY ("comics" | "collectibles" | "" = les deux), PROBE_ONLY ("true" pour
      ne faire que la sonde MCP et s'arreter).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GRAPHQL_URL = "https://web.api.prod.veve.me/graphql"
HEADERS = {
    "content-type": "application/json",
    "client-name": "veve-app-web-server",
    "client-version": "1.0",
    "client-operation": "publicStoreCollectibleEditionsQuery",
    "x-auth-version": "2",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

COMICS_TAB = "🟢C-COMICS"
COLLECT_TAB = "🔵C-COLLECTIBLE"

# PAS de mcp_priority : le cout en points de l'acces prioritaire est CONSTANT
# (5 000) et VeVe ne l'expose nulle part (30 champs sondes). La sonde tourne quand
# meme — si VeVe l'exposait un jour, le log le dirait.
NEW_COLS = ["supply", "store_price_gems", "veve_exclusive"]

# Noms candidats pour les points MCP a depenser (acces prioritaire au drop).
MCP_CANDIDATES = [
    "mcpPriorityCost", "mcpPriorityPoints", "mcpPriority", "priorityMcpPoints",
    "priorityMcpCost", "priorityCost", "mcpCost", "mcpPoints", "mcpSpend",
    "requiredMcpPoints", "mcpThreshold", "dropMcpPoints", "priorityAccessMcp",
    "mcpEntryCost", "priorityPassCost",
]

TIMEOUT = 30
RETRIES = 3
BACKOFF = 2
PAUSE = 0.05

_EXCLUSIVE_RE = re.compile(r"veve[^a-z0-9]{0,3}exclusive", re.IGNORECASE)


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

def _post(query: str, item_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Renvoie (data, erreur). Ne leve jamais."""
    payload = {"operationName": "publicStoreCollectibleEditionsQuery",
               "variables": {"id": item_id}, "query": query}
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(GRAPHQL_URL, json=payload, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                body = r.json()
                if body.get("errors"):
                    return None, str(body["errors"][0].get("message", ""))[:120]
                return body.get("data") or {}, ""
            last = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code == 400:      # champ inconnu -> inutile de reessayer
                return None, last
        except Exception as e:            # reseau : on retente avec backoff
            last = str(e)[:120]
        time.sleep(BACKOFF * attempt)
    return None, last


def probe_mcp_fields(sample_comic_id: str, sample_collectible_id: str) -> Dict[str, str]:
    """Teste les noms candidats un par un. Renvoie {'comic': champ, 'collectible': champ}."""
    found: Dict[str, str] = {}
    for kind, root, sample in (("comic", "publicComicType", sample_comic_id),
                               ("collectible", "publicCollectibleType", sample_collectible_id)):
        if not sample:
            continue
        for field in MCP_CANDIDATES:
            q = ("query publicStoreCollectibleEditionsQuery($id: ID!){ "
                 f"{root}(id:$id){{ id {field} }} }}")
            data, err = _post(q, sample)
            if data is not None:
                val = (data.get(root) or {}).get(field)
                print(f"  ✔ {kind}.{field} EXISTE (valeur sur l'echantillon : {val!r})",
                      flush=True)
                found[kind] = field
                break
            if "cannot query field" not in err.lower() and "Cannot query field" not in err:
                print(f"    {kind}.{field} -> {err}", flush=True)
        if kind not in found:
            print(f"  ✘ aucun champ MCP trouve pour {kind} "
                  f"({len(MCP_CANDIDATES)} noms testes)", flush=True)
    return found


def _query(root: str, mcp_field: str) -> str:
    extra = f" {mcp_field}" if mcp_field else ""
    return ("query publicStoreCollectibleEditionsQuery($id: ID!){ "
            f"{root}(id:$id){{ id totalIssued storePrice description{extra} }} }}")


def _prix(x: Any, is_comic: bool) -> Any:
    """Le prix boutique ramene en GEMS (1 gem ~ 1 $).

    VeVe melange DEUX echelles dans `storePrice` cote COMICS :
      - vieux comics vendus en GEMS           -> 10, 15, 20
      - comics recents vendus en FIAT, CENTIMES -> 699, 798, 1499
    Preuves : Captain America Comics #7 = 699 et la carte Discord de Preda dit
    « 7 gems » ; sur Cheetara #2, my-nft-tracker dit 7.98 quand GraphQL dit 798.
    Au-dela de 100 = centimes (un comic n'a jamais coute 100 gems). IDEMPOTENT.
    ⚠️ Pas pour les collectibles : un collectible a 1 500 gems, ça existe.
    """
    if x in (None, ""):
        return x
    try:
        v = float(x)
    except (TypeError, ValueError):
        return x
    return round(v / 100, 2) if (is_comic and v >= 100) else v


def fetch_one(root: str, item_id: str, mcp_field: str) -> Dict[str, Any]:
    data, err = _post(_query(root, mcp_field), item_id)
    if data is None:
        return {"_error": err}
    node = data.get(root) or {}
    if not node:
        return {"_error": "entity not found"}
    out: Dict[str, Any] = {
        "supply": node.get("totalIssued"),
        "store_price_gems": _prix(node.get("storePrice"),
                                  root == "publicComicType"),
        "description": node.get("description") or "",
    }
    if mcp_field:
        out["mcp_priority"] = node.get(mcp_field)
    return out


def fetch_many(root: str, ids: List[str], mcp_field: str, workers: int) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    done = 0
    total = len(ids)

    def task(i: str):
        time.sleep(PAUSE)
        return i, fetch_one(root, i, mcp_field)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for item_id, cols in ex.map(task, ids):
            out[item_id] = cols
            done += 1
            if done % 200 == 0 or done == total:
                ko = sum(1 for c in out.values() if c.get("_error"))
                print(f"    {done}/{total} enrichis ({ko} en echec)", flush=True)
    return out


# ---------------------------------------------------------------------------
# Sheet
# ---------------------------------------------------------------------------

def _client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON manquant.")
    return gspread.authorize(
        Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES))


def _a1_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _header(ws) -> List[str]:
    """En-tete de l'onglet, lu de facon FIABLE.

    `row_values(1)` a renvoye une liste vide au 1er run (le daily reecrivait le
    catalogue au meme moment : sync_catalogue efface puis reecrit A1, et une
    lecture tombee dans cette fenetre voit une ligne 1 vide). On relit donc
    jusqu'a 3 fois, et on echoue en AFFICHANT ce qu'on a vu — pas sur un
    `ValueError: 'series_uuid' is not in list` qui n'apprend rien.
    """
    for attempt in range(1, 4):
        rows = ws.get("A1:BZ1")
        header = [str(c).strip() for c in (rows[0] if rows else [])]
        if "veve_uuid" in header:
            return header
        print(f"  en-tete {ws.title} illisible (essai {attempt}/3) : {header[:6]}",
              flush=True)
        time.sleep(5)
    raise RuntimeError(
        f"En-tete de {ws.title} introuvable. Le daily etait peut-etre en train de "
        f"reecrire le catalogue — relancer le workflow dans une minute.")


def _ensure_headers(ws) -> List[str]:
    """Garantit la presence des colonnes dans l'en-tete (ajoutees a la fin si
    sheets.py n'a pas encore tourne). Renvoie l'en-tete a jour."""
    header = _header(ws)
    missing = [c for c in NEW_COLS if c not in header]
    if not missing:
        return header
    # veve_exclusive n'a de sens que pour les comics
    if ws.title == COLLECT_TAB:
        missing = [c for c in missing if c != "veve_exclusive"]
        if not missing:
            return header
    start = len(header) + 1
    if start + len(missing) - 1 > ws.col_count:
        ws.add_cols(start + len(missing) - 1 - ws.col_count)
    rng = f"{_a1_col(start)}1:{_a1_col(start + len(missing) - 1)}1"
    ws.update(range_name=rng, values=[missing], value_input_option="RAW")
    print(f"  en-tete {ws.title} : colonnes ajoutees {missing}", flush=True)
    return header + missing


def _write_column(ws, header: List[str], col_name: str, values: Dict[int, Any]) -> int:
    """Ecrit UNE colonne, uniquement les lignes fournies. `values` : {ligne: valeur}."""
    if col_name not in header or not values:
        return 0
    idx = header.index(col_name) + 1
    letter = _a1_col(idx)
    lo, hi = min(values), max(values)
    # On relit la colonne (UNFORMATTED : locale FR, "6,99" numerise donnerait 699 —
    # bug majeur deja paye le 10/07) pour ne PAS ecraser les cellules hors `values`.
    current = ws.get(f"{letter}{lo}:{letter}{hi}",
                     value_render_option="UNFORMATTED_VALUE")
    grid: List[List[Any]] = []
    for row in range(lo, hi + 1):
        if row in values:
            grid.append([values[row]])
        else:
            i = row - lo
            cell = current[i][0] if i < len(current) and current[i] else ""
            grid.append([cell])
    ws.update(range_name=f"{letter}{lo}:{letter}{hi}", values=grid,
              value_input_option="RAW")
    return len(values)


def backfill_tab(sh, tab: str, root: str, id_col: str, mcp_field: str,
                 workers: int, max_items: int, write_every: int,
                 force: bool = False) -> Dict[str, int]:
    ws = sh.worksheet(tab)
    header = _ensure_headers(ws)
    rows = ws.get_all_records()
    print(f"\n{tab} : {len(rows)} lignes{' [FORCE]' if force else ''}", flush=True)

    # Lignes a remplir, groupees par id VeVe (pour les comics : 1 appel par SERIE,
    # applique aux ~5 lignes de rarete de cette serie).
    todo: Dict[str, List[int]] = {}
    excl_now: Dict[int, Any] = {}
    for i, row in enumerate(rows):
        line = i + 2                                   # +1 en-tete, +1 base 1
        item_id = str(row.get(id_col, "") or "").strip()
        # la cover exclusive se lit dans la description DEJA presente : gratuit
        if tab == COMICS_TAB and (force
                                  or str(row.get("veve_exclusive", "")).strip() == ""):
            desc = row.get("description")
            if desc not in (None, ""):
                excl_now[line] = bool(_EXCLUSIVE_RE.search(str(desc)))
        if not item_id:
            continue
        # deja fait -> on ne redemande pas (c'est la reprise). FORCE=true rouvre
        # tout : indispensable au 1er passage, car le daily du 13/07 a ecrit dans
        # `supply` des comics de la fenetre le releaseAmount du tracker, qui est le
        # supply de la RARETE et non celui de la SERIE (Cheetara #2 COMMON = 400).
        if not force and str(row.get("supply", "")).strip() != "":
            continue
        todo.setdefault(item_id, []).append(line)

    if excl_now:
        n = _write_column(ws, header, "veve_exclusive", excl_now)
        print(f"  veve_exclusive (depuis les descriptions deja en base) : {n} lignes",
              flush=True)

    ids = list(todo)
    if max_items:
        ids = ids[:max_items]
    print(f"  a enrichir : {len(ids)} {'series' if tab == COMICS_TAB else 'items'} "
          f"({sum(len(todo[i]) for i in ids)} lignes)", flush=True)
    if not ids:
        return {"ids": 0, "rows": 0}

    filled_rows = 0
    for chunk_start in range(0, len(ids), write_every):
        chunk = ids[chunk_start:chunk_start + write_every]
        got = fetch_many(root, chunk, mcp_field, workers)

        cols: Dict[str, Dict[int, Any]] = {c: {} for c in NEW_COLS}
        for item_id, data in got.items():
            if data.get("_error"):
                continue
            for line in todo[item_id]:
                if data.get("supply") not in (None, ""):
                    cols["supply"][line] = data["supply"]
                if data.get("store_price_gems") not in (None, ""):
                    cols["store_price_gems"][line] = data["store_price_gems"]
                if data.get("mcp_priority") not in (None, ""):
                    cols["mcp_priority"][line] = data["mcp_priority"]
                if tab == COMICS_TAB and data.get("description"):
                    cols["veve_exclusive"][line] = bool(
                        _EXCLUSIVE_RE.search(str(data["description"])))

        for col_name, values in cols.items():
            written = _write_column(ws, header, col_name, values)
            if col_name == "supply":
                filled_rows += written
        print(f"  lot ecrit : {chunk_start + len(chunk)}/{len(ids)} "
              f"({filled_rows} lignes remplies)", flush=True)

    return {"ids": len(ids), "rows": filled_rows}


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERROR: SHEET_ID manquant.", file=sys.stderr)
        return 2
    workers = int(os.environ.get("WORKERS", "6"))
    max_items = int(os.environ.get("MAX_ITEMS", "0"))
    write_every = int(os.environ.get("WRITE_EVERY", "400"))
    only = os.environ.get("ONLY", "").strip().lower()
    probe_only = os.environ.get("PROBE_ONLY", "").strip().lower() == "true"
    force = os.environ.get("FORCE", "").strip().lower() == "true"

    sh = _client().open_by_key(sheet_id)

    # --- echantillons pour la sonde : la 1re ligne de chaque catalogue ---
    comics_ws = sh.worksheet(COMICS_TAB)
    collect_ws = sh.worksheet(COLLECT_TAB)
    c_header = _header(comics_ws)
    k_header = _header(collect_ws)
    sample_comic = comics_ws.cell(2, c_header.index("series_uuid") + 1).value
    sample_collectible = collect_ws.cell(2, k_header.index("veve_uuid") + 1).value

    print("Sonde du champ MCP (points a depenser pour l'acces prioritaire) :", flush=True)
    mcp = probe_mcp_fields(sample_comic, sample_collectible)
    if not mcp:
        print("=> VeVe n'expose aucun champ MCP prioritaire : colonne mcp_priority "
              "laissee MANUELLE (elle ne sera jamais ecrasee).", flush=True)
    if probe_only:
        return 0

    total = {"ids": 0, "rows": 0}
    if only in ("", "comics"):
        r = backfill_tab(sh, COMICS_TAB, "publicComicType", "series_uuid",
                         mcp.get("comic", ""), workers, max_items, write_every, force)
        total = {k: total[k] + r[k] for k in total}
    if only in ("", "collectibles"):
        r = backfill_tab(sh, COLLECT_TAB, "publicCollectibleType", "veve_uuid",
                         mcp.get("collectible", ""), workers, max_items, write_every,
                         force)
        total = {k: total[k] + r[k] for k in total}

    print(f"\nTermine : {total['ids']} fiches VeVe interrogees, "
          f"{total['rows']} lignes remplies, en {time.time() - t0:.0f}s.", flush=True)
    print("Relancer le workflow reprend exactement la ou il s'est arrete "
          "(les lignes deja remplies sont ignorees).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
