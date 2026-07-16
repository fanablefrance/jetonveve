"""
StackR pseudo <-> wallet tracker.

StackR (stackr.world) est la place de marché OMI officielle pour VeVe : chaque
compte StackR est lié à un compte VeVe, ce qui permet de relier une **adresse
wallet** (celle qu'on voit sur CollectChain / dans les onglets Chain*) au
**pseudo VeVe**.

API découverte (tRPC, https://www.stackr.world/api/trpc/) :

  publicVeve.getAllLeaderboards        - sans cookie. Top holders par marque :
                                         wallet (imx) + username.
  publicVeve.getLeaderboardRanking     - sans cookie mais les usernames ne sont
                                         renvoyés QU'AVEC une session. Classement
                                         OMI rewards paginé (scope MONTH/WEEK,
                                         skip_periods pour remonter le temps).
  verifiedVeve.getPublicUser           - NECESSITE un cookie de session anonyme
        {id: <username | wallet>}        (obtenu en visitant la home). Renvoie
                                         veve_user_id, imx_wallet,
                                         web3_smart_wallet (PAS le username).
  verifiedVeve.getPublicUserListings   - {veveUserId,...} -> rows avec username
                                         + imx_wallet du vendeur.
  verifiedVeve.getPublicUserTransactions - {web3SmartWallet,...} -> ventes avec
                                         seller/buyer (smart wallets) ET
                                         seller_username/buyer_username.

Deux familles de wallets par utilisateur :
  imx_wallet         = le wallet VeVe historique (IMX -> CollectChain). C'est
                       LUI qui apparaît dans ChainActivity.
  web3_smart_wallet  = le wallet StackR (Base) utilisé pour payer en OMI.

Onglet Sheet `Pseudos` (réécrit à chaque run) :
  username | wallet_imx | wallet_stackr | veve_user_id | status | source |
  first_seen | last_checked

  status : ok          -> pseudo trouvé
           no_username -> compte StackR trouvé mais pseudo pas encore découvert
                          (aucun listing / aucune vente) ; re-testé après
                          RECHECK_NO_USERNAME_DAYS
           not_found   -> wallet inconnu de StackR (pas de compte lié) ;
                          re-testé après RECHECK_NOT_FOUND_DAYS

Sources d'un run quotidien :
  1. leaderboard   : getAllLeaderboards (gratuit, sans session).
  2. ranking       : pages du classement OMI (si session) -> pseudos garantis.
  3. chain         : wallets les plus actifs de ChainActivity
                     encore inconnus -> résolution individuelle.
  4. transactions  : contreparties découvertes dans les ventes des wallets
                     résolus (pseudo garanti).

Env :
  SHEET_ID                    id du spreadsheet (obligatoire hors --test)
  STACKR_MAX_LOOKUPS          budget d'appels verifiedVeve par run (def. 200)
  STACKR_RANKING_PAGES        pages de classement moissonnées par période (def. 3)
  STACKR_PAUSE                pause entre appels en secondes (def. 0.35)

Test local (sans écrire dans le Sheet) :
  python -m scraper.stackr --test
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

BASE = "https://www.stackr.world"
TRPC = BASE + "/api/trpc/"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

PSEUDOS_TAB = "🟣C-PSEUDOS"
# The last 2 columns are the on-chain WALLET REGISTRY, filled by
# scraper.wallet_registry from ChainActivity (kept across runs; chain_first_seen
# only ever moves earlier — approximates the wallet's creation).
# ORDRE VOULU PAR PREDA (16/07) : ses colonnes de tete d'abord (username · Type
# de compte · holdings · collectorScore · activityStatus · engagementLevel ·
# value_store · value_floor), puis l'identite/wallets, le reste du profil, les
# rangs. Reordonner est SANS RISQUE : tous les writers reconstruisent par NOM
# (r.get(c, "")) ; le seul acces positionnel est col_values(1) = username, qui
# reste en colonne A. La colonne manuelle « Type de compte » (menu deroulant)
# PERSISTE au rebuild (relecture de l'existant puis reconstruction par nom).
PSEUDOS_HEADER = [
    "username", "Type de compte",
    # profil (injecte par scraper.ledger) — les plus regardes en premier
    "holdings", "collectorScore", "activityStatus", "engagementLevel",
    "value_store", "value_floor",
    # --- identite / wallets ---
    "wallet_imx", "wallet_stackr", "veve_user_id", "status", "source",
    "first_seen", "last_checked", "chain_first_seen", "chain_last_active",
    # --- profil (suite) ---
    "distinct_collectibles", "acquired", "sold", "retention", "median_hold_days",
    "qty_bucket", "airdropOnly",
    # --- rangs whales (l'onglet 🐋A-WHALES est absorbe ici ; vide = hors top 100) ---
    "rang_qty", "rang_floor", "rang_store",
]

PAUSE = float(os.environ.get("STACKR_PAUSE", "0.35"))
MAX_LOOKUPS = int(os.environ.get("STACKR_MAX_LOOKUPS", "200"))
RANKING_PAGES = int(os.environ.get("STACKR_RANKING_PAGES", "3"))
RECHECK_NOT_FOUND_DAYS = 30
RECHECK_NO_USERNAME_DAYS = 14


# ---------------------------------------------------------------------------
# tRPC client
# ---------------------------------------------------------------------------

class StackrClient:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.lookups = 0            # nb d'appels verifiedVeve consommés
        self.verified_ok = False    # session acceptée par verifiedVeve ?

    # -- bas niveau --------------------------------------------------------

    def _get(self, proc: str, payload: Any) -> Optional[Any]:
        """GET tRPC ; renvoie result.data.json ou None (erreur / redirect)."""
        if payload is None:
            inp = {"json": None, "meta": {"values": ["undefined"], "v": 1}}
        else:
            inp = {"json": payload}
        url = TRPC + proc + "?input=" + urllib.parse.quote(json.dumps(inp))
        try:
            r = self.s.get(url, timeout=30, allow_redirects=False)
        except requests.RequestException as e:
            print(f"    [stackr] {proc}: {e}", flush=True)
            return None
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            return None
        try:
            return r.json()["result"]["data"]["json"]
        except Exception:
            return None

    def _verified(self, proc: str, payload: Any) -> Optional[Any]:
        """Appel verifiedVeve.* : compte dans le budget + pause de politesse."""
        self.lookups += 1
        time.sleep(PAUSE)
        return self._get(proc, payload)

    # -- bootstrap ----------------------------------------------------------

    def _probe(self, label: str) -> bool:
        """Teste l'accès verifiedVeve et loggue le diagnostic."""
        inp = {"json": {"id": "DrFuze"}}
        url = (TRPC + "verifiedVeve.getPublicUser?input="
               + urllib.parse.quote(json.dumps(inp)))
        try:
            r = self.s.get(url, timeout=30, allow_redirects=False)
        except requests.RequestException as e:
            print(f"    [stackr] probe({label}): {e}", flush=True)
            return False
        loc = r.headers.get("location", "")
        ct = r.headers.get("content-type", "")
        ok = r.status_code == 200 and "json" in ct and '"imx_wallet"' in r.text
        print(f"    [stackr] probe({label}): status={r.status_code} "
              f"ct={ct.split(';')[0]}{' -> ' + loc if loc else ''} "
              f"{'OK' if ok else 'KO'}", flush=True)
        return ok

    def bootstrap(self) -> bool:
        """Obtient un accès aux endpoints verifiedVeve (les publicVeve
        fonctionnent dans tous les cas).

        Si STACKR_COOKIE est fourni (secret GitHub : coller l'en-tête `cookie`
        d'une requête `trpc` depuis DevTools → Network → Request Headers), on
        l'envoie **tel quel** dans l'en-tête `Cookie` de chaque requête. On ne
        le reparse PAS : les jetons Privy (`privy-token`, `privy-session`) sont
        de longs JWT/valeurs URL-encodées que tout reparsing corromprait
        (→ 500 côté serveur). À défaut de cookie, on tente une session
        anonyme récoltée sur la home."""
        env_cookie = os.environ.get("STACKR_COOKIE", "").strip()
        if env_cookie:
            # header brut sur TOUTES les requêtes de la session
            self.s.headers["Cookie"] = env_cookie
            if self._probe("env_cookie"):
                self.verified_ok = True
                return True
            # au cas où l'utilisateur aurait collé "cookie: xxx"
            if env_cookie.lower().startswith("cookie:"):
                self.s.headers["Cookie"] = env_cookie.split(":", 1)[1].strip()
                if self._probe("env_cookie_stripped"):
                    self.verified_ok = True
                    return True
            self.s.headers.pop("Cookie", None)

        try:
            r = self.s.get(BASE + "/", timeout=30)
            print(f"    [stackr] home: {r.status_code}, "
                  f"cookies={list(self.s.cookies.keys())}", flush=True)
        except requests.RequestException as e:
            print(f"    [stackr] bootstrap: {e}", flush=True)
        if self._probe("after_home"):
            self.verified_ok = True
            return True

        self.verified_ok = False
        return False

    # -- endpoints ----------------------------------------------------------

    def all_leaderboards(self) -> Dict[str, List[Dict[str, Any]]]:
        return self._get("publicVeve.getAllLeaderboards", None) or {}

    def ranking_page(self, scope: str = "MONTH", skip_periods: int = 0,
                     page: int = 1) -> Dict[str, Any]:
        payload = {"scope": scope, "skip_periods": skip_periods}
        if page > 1:
            payload["page"] = page
        return self._get("publicVeve.getLeaderboardRanking", payload) or {}

    def get_public_user(self, id_: str) -> Optional[Dict[str, Any]]:
        """id_ = username OU wallet (imx ou smart). Renvoie
        {veve_user_id, imx_wallet, web3_smart_wallet, ...} ou None."""
        out = self._verified("verifiedVeve.getPublicUser", {"id": id_})
        return out if isinstance(out, dict) else None

    def user_listings(self, veve_user_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        out = self._verified("verifiedVeve.getPublicUserListings", {
            "veveUserId": veve_user_id, "sortBy": "created_at",
            "sortDirection": "desc", "search": "", "limit": limit, "cursor": 0,
        })
        return (out or {}).get("data") or []

    def user_transactions(self, smart_wallet: str, limit: int = 10) -> List[Dict[str, Any]]:
        out = self._verified("verifiedVeve.getPublicUserTransactions", {
            "web3SmartWallet": smart_wallet, "limit": limit, "cursor": 0,
        })
        return (out or {}).get("data") or []


# ---------------------------------------------------------------------------
# Logique de résolution
# ---------------------------------------------------------------------------

def _now() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _days_since(stamp: str) -> float:
    try:
        then = _dt.datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
        return (_dt.datetime.utcnow() - then).total_seconds() / 86400
    except Exception:
        return 1e9


def _norm(w: Any) -> str:
    return str(w or "").strip().lower()


class PseudoBook:
    """Le mapping en mémoire, clé = wallet_imx (fallback wallet_stackr)."""

    def __init__(self, existing_rows: List[Dict[str, Any]]) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        # Rows WITHOUT a wallet yet (e.g. pseudos found via the Market) are kept
        # aside and preserved verbatim — a found pseudo must never be dropped just
        # because StackR hasn't resolved its wallet.
        self.extra: List[Dict[str, Any]] = []
        for r in existing_rows:
            key = _norm(r.get("wallet_imx")) or _norm(r.get("wallet_stackr"))
            if key:
                self.rows[key] = dict(r)
            elif str(r.get("veve_user_id", "")).strip() or str(r.get("username", "")).strip():
                self.extra.append(dict(r))
        # index secondaire par smart wallet
        self.by_smart = {_norm(r.get("wallet_stackr")): k
                         for k, r in self.rows.items() if _norm(r.get("wallet_stackr"))}
        self.new_rows = 0
        self.usernames_found = 0

    def known(self, wallet: str) -> Optional[Dict[str, Any]]:
        w = _norm(wallet)
        if w in self.rows:
            return self.rows[w]
        if w in self.by_smart:
            return self.rows[self.by_smart[w]]
        return None

    def needs_lookup(self, wallet: str) -> bool:
        r = self.known(wallet)
        if r is None:
            return True
        st = str(r.get("status", ""))
        if st == "ok":
            return False
        age = _days_since(r.get("last_checked", ""))
        if st == "not_found":
            return age > RECHECK_NOT_FOUND_DAYS
        return age > RECHECK_NO_USERNAME_DAYS  # no_username / inconnu

    def upsert(self, *, username: str = "", wallet_imx: str = "",
               wallet_stackr: str = "", veve_user_id: str = "",
               status: str = "", source: str = "") -> None:
        key = _norm(wallet_imx) or _norm(wallet_stackr)
        if not key:
            return
        now = _now()
        row = self.rows.get(key) or (self.by_smart.get(_norm(wallet_stackr))
                                     and self.rows[self.by_smart[_norm(wallet_stackr)]])
        if row is None:
            row = {"username": "", "wallet_imx": "", "wallet_stackr": "",
                   "veve_user_id": "", "status": "", "source": source,
                   "first_seen": now, "last_checked": now}
            self.rows[key] = row
            self.new_rows += 1
        had_username = bool(str(row.get("username", "")).strip())
        if username and not had_username:
            self.usernames_found += 1
        if username:
            row["username"] = username
        if wallet_imx:
            row["wallet_imx"] = _norm(wallet_imx)
        if wallet_stackr:
            row["wallet_stackr"] = _norm(wallet_stackr)
            self.by_smart[_norm(wallet_stackr)] = _norm(row.get("wallet_imx")) or key
        if veve_user_id:
            row["veve_user_id"] = veve_user_id
        if status:
            # ne jamais rétrograder un "ok"
            if row.get("status") != "ok" or status == "ok":
                row["status"] = status
        if str(row.get("username", "")).strip():
            row["status"] = "ok"
        if source and not row.get("source"):
            row["source"] = source
        row["last_checked"] = now

    def as_grid(self) -> List[List[Any]]:
        recs = sorted(self.rows.values(),
                      key=lambda r: (r.get("status") != "ok",
                                     str(r.get("username", "")).lower() or "~",
                                     str(r.get("wallet_imx", ""))))
        # Preserve wallet-less rows (Market pseudos) that aren't already covered
        # by a wallet-keyed row (matched by veve_user_id or username).
        seen_uid = {str(r.get("veve_user_id", "")).strip() for r in recs
                    if str(r.get("veve_user_id", "")).strip()}
        seen_name = {str(r.get("username", "")).strip().lower() for r in recs
                     if str(r.get("username", "")).strip()}
        extra = [r for r in self.extra
                 if str(r.get("veve_user_id", "")).strip() not in seen_uid
                 and str(r.get("username", "")).strip().lower() not in seen_name]
        return [PSEUDOS_HEADER] + [[r.get(c, "") for c in PSEUDOS_HEADER]
                                   for r in (recs + extra)]


def _resolve_wallet(cli: StackrClient, book: PseudoBook, wallet: str,
                    source: str, username_hint: str = "") -> None:
    """Résout un wallet/username : getPublicUser puis, si besoin, cherche le
    pseudo via listings puis transactions. Moissonne aussi les contreparties."""
    user = cli.get_public_user(wallet)
    if not user:
        book.upsert(wallet_imx=wallet if not username_hint else "",
                    wallet_stackr="", status="not_found", source=source)
        return
    imx = _norm(user.get("imx_wallet"))
    smart = _norm(user.get("web3_smart_wallet"))
    uid = str(user.get("veve_user_id") or "")
    username = username_hint or str(user.get("username") or "")

    if not username and uid and cli.lookups < MAX_LOOKUPS:
        for row in cli.user_listings(uid, limit=5):
            u = str(row.get("username") or "")
            if u and _norm(row.get("imx_wallet")) in (imx, ""):
                username = u
                break

    if not username and smart and cli.lookups < MAX_LOOKUPS:
        for tx in cli.user_transactions(smart, limit=10):
            seller, buyer = _norm(tx.get("seller")), _norm(tx.get("buyer"))
            su = str(tx.get("seller_username") or "")
            bu = str(tx.get("buyer_username") or "")
            if seller == smart and su:
                username = su
            elif buyer == smart and bu:
                username = bu
            # contreparties : pseudo garanti, wallet smart connu ; on résout
            # aussi leur wallet imx (jointure Chain*) si le budget le permet
            for w, u in ((seller, su), (buyer, bu)):
                if u and w and w != smart and book.known(w) is None:
                    cp_imx = ""
                    cp_uid = ""
                    if cli.lookups < MAX_LOOKUPS:
                        cp = cli.get_public_user(w)
                        if cp:
                            cp_imx = _norm(cp.get("imx_wallet"))
                            cp_uid = str(cp.get("veve_user_id") or "")
                    book.upsert(username=u, wallet_imx=cp_imx,
                                wallet_stackr=w, veve_user_id=cp_uid,
                                status="ok", source="transactions")

    book.upsert(username=username, wallet_imx=imx, wallet_stackr=smart,
                veve_user_id=uid,
                status="ok" if username else "no_username", source=source)


def harvest(cli: StackrClient, book: PseudoBook,
            chain_wallets: List[str]) -> Dict[str, Any]:
    """Exécute les 4 sources dans l'ordre. chain_wallets = wallets CollectChain
    triés du plus actif au moins actif."""
    note = []

    # 1. Leaderboards top holders (sans session, wallet = imx)
    lb = cli.all_leaderboards()
    lb_pairs = 0
    for entries in (lb or {}).values():
        for e in entries:
            u, w = str(e.get("username") or ""), _norm(e.get("wallet"))
            if u and w:
                book.upsert(username=u, wallet_imx=w, status="ok",
                            source="leaderboard")
                lb_pairs += 1
    note.append(f"leaderboard_pairs={lb_pairs}")

    if not cli.verified_ok:
        note.append("verifiedVeve=OFF (session refusée) — sources 2-4 sautées")
        return {"note": "; ".join(note)}

    # 2. Classement OMI rewards (usernames présents avec session)
    rank_new = 0
    for skip in (0, 1):
        for page in range(1, RANKING_PAGES + 1):
            if cli.lookups >= MAX_LOOKUPS:
                break
            data = cli.ranking_page("MONTH", skip, page)
            items = (data or {}).get("items") or []
            if not items:
                break
            for it in items:
                u = str(it.get("username") or "")
                addr = _norm(it.get("address"))
                if not addr:
                    continue
                if u and book.needs_lookup(addr) and cli.lookups < MAX_LOOKUPS:
                    _resolve_wallet(cli, book, addr, "ranking", username_hint=u)
                    rank_new += 1
            if len(items) < 20:
                break
    note.append(f"ranking_resolved={rank_new}")

    # 3. Wallets CollectChain encore inconnus (les plus actifs d'abord)
    chain_done = 0
    for w in chain_wallets:
        if cli.lookups >= MAX_LOOKUPS - 2:   # garder un peu de marge
            break
        if not book.needs_lookup(w):
            continue
        _resolve_wallet(cli, book, w, "chain")
        chain_done += 1
    note.append(f"chain_resolved={chain_done}")

    return {"note": "; ".join(note)}


# ---------------------------------------------------------------------------
# Google Sheet
# ---------------------------------------------------------------------------

def read_existing(spreadsheet_id: str) -> List[Dict[str, Any]]:
    from scraper.sheets import _client
    sh = _client().open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(PSEUDOS_TAB)
    except Exception:
        return []
    return ws.get_all_records() if ws.row_count > 1 else []


def read_chain_wallets(spreadsheet_id: str) -> List[str]:
    """Wallets vus on-chain, triés par activité décroissante."""
    from scraper.sheets import _client
    sh = _client().open_by_key(spreadsheet_id)
    totals: Dict[str, float] = {}
    try:
        ws = sh.worksheet("ChainActivity")
        rows = ws.get_all_records() if ws.row_count > 1 else []
        for r in rows:
            a = _norm(r.get("account"))
            if a.startswith("0x"):
                try:
                    totals[a] = totals.get(a, 0) + float(r.get("total") or 0)
                except (TypeError, ValueError):
                    totals[a] = totals.get(a, 0)
    except Exception as e:
        print(f"    ChainActivity illisible: {e}", flush=True)
    return [w for w, _ in sorted(totals.items(), key=lambda kv: -kv[1])]


def write_book(spreadsheet_id: str, book: PseudoBook) -> None:
    from scraper.sheets import _client, _open_worksheet
    sh = _client().open_by_key(spreadsheet_id)
    ws = _open_worksheet(sh, PSEUDOS_TAB, cols=len(PSEUDOS_HEADER))
    grid = book.as_grid()
    ws.clear()
    ws.update(range_name="A1", values=grid, value_input_option="RAW")
    try:
        ws.freeze(rows=1, cols=2)      # username + Type de compte figes (Preda)
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


# Menu deroulant de la colonne manuelle "Type de compte" (choix Preda 16/07).
PSEUDO_TYPES = ["VeVe Team", "Fondateur", "Modération", "Publisher",
                "Influenceur", "À suivre"]

# Fond leger par type de compte (demande Preda 16/07) — pour reperer le profil
# d'un coup d'oeil. Pastels sur fond blanc, dans l'esprit du reste du Sheet.
PSEUDO_TYPE_COLORS = {
    "VeVe Team":   {"red": 0.80, "green": 0.89, "blue": 0.99},   # bleu
    "Fondateur":   {"red": 0.99, "green": 0.91, "blue": 0.67},   # or
    "Modération":  {"red": 0.85, "green": 0.94, "blue": 0.83},   # vert
    "Publisher":   {"red": 0.90, "green": 0.82, "blue": 0.94},   # violet
    "Influenceur": {"red": 0.99, "green": 0.87, "blue": 0.80},   # orange
    "À suivre":    {"red": 0.91, "green": 0.91, "blue": 0.91},   # gris
}


def apply_type_validation(sh, ws) -> None:
    """(Re)pose le menu deroulant (6 categories) sur la colonne manuelle
    "Type de compte", sur toutes les lignes de donnees. La validation survit aux
    reecritures de valeurs (ws.clear/update ne touche que les valeurs, pas la
    data-validation) ; on la repose chaque jour par securite. Non bloquant.
    ⚠️ On BORNE la plage a la taille REELLE de la grille (relue a la source) —
    une plage plus grande que la grille fait refuser TOUTE la requete par l'API
    (« range exceeds grid limits ») et le menu ne serait jamais pose."""
    try:
        col = PSEUDOS_HEADER.index("Type de compte")
    except ValueError:
        return
    try:
        n_rows = int(sh.worksheet(ws.title).row_count or 0)
    except Exception:                                      # noqa: BLE001
        n_rows = int(getattr(ws, "row_count", 0) or 0)
    end = max(2, n_rows)                                   # au moins la 1re ligne de donnees
    req = {"setDataValidation": {
        "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": v}
                                     for v in PSEUDO_TYPES]},
            "showCustomUi": True, "strict": True}}}
    try:
        sh.batch_update({"requests": [req]})
    except Exception as e:                                  # noqa: BLE001
        print(f"    validation 'Type de compte' : {e}", flush=True)


def apply_type_colors(sh, ws) -> None:
    """Fond leger par type de compte (conditional formatting) : on repere le
    profil d'un coup d'oeil. On PURGE d'abord les regles existantes de l'onglet
    (sinon elles s'empilent a chaque run), puis une regle par valeur. Plage
    bornee a la grille reelle (meme raison que la validation). Non bloquant."""
    try:
        col = PSEUDOS_HEADER.index("Type de compte")
    except ValueError:
        return
    try:
        n_rows = int(sh.worksheet(ws.title).row_count or 0)
    except Exception:                                      # noqa: BLE001
        n_rows = int(getattr(ws, "row_count", 0) or 0)
    end = max(2, n_rows)
    rng = {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
           "startColumnIndex": col, "endColumnIndex": col + 1}
    reqs = []
    try:                                                   # purge des anciennes regles
        meta = sh.fetch_sheet_metadata()
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("sheetId") == ws.id:
                for _ in range(len(s.get("conditionalFormats", []) or [])):
                    reqs.append({"deleteConditionalFormatRule":
                                 {"sheetId": ws.id, "index": 0}})
                break
    except Exception:                                      # noqa: BLE001
        pass
    for val, color in PSEUDO_TYPE_COLORS.items():
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [rng],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ",
                              "values": [{"userEnteredValue": val}]},
                "format": {"backgroundColor": color}}}}})
    try:
        sh.batch_update({"requests": reqs})
    except Exception as e:                                  # noqa: BLE001
        print(f"    couleurs 'Type de compte' : {e}", flush=True)


def append_runlog(spreadsheet_id: str, summary: Dict[str, Any]) -> None:
    """Pseudos-run entry in the unified Logs tab."""
    from scraper.sheets import append_log, summary_details
    append_log(spreadsheet_id, "pseudos", str(summary.get("status", "")),
               summary_details(summary, skip=("status", "run_at_utc")))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    test = "--test" in sys.argv

    cli = StackrClient()
    ok = cli.bootstrap()
    print(f"StackR verifiedVeve API: {'OK' if ok else 'INDISPONIBLE (session refusée)'}",
          flush=True)

    if test:
        book = PseudoBook([])
        res = harvest(cli, book, chain_wallets=[])
        print(f"note: {res['note']}")
        print(f"rows={len(book.rows)} usernames={book.usernames_found} "
              f"lookups={cli.lookups}")
        for r in list(book.rows.values())[:15]:
            print(f"  {r.get('username') or '?':24s} imx={r.get('wallet_imx')} "
                  f"stackr={r.get('wallet_stackr')}")
        return 0

    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not sheet_id:
        print("SHEET_ID env var is not set.", file=sys.stderr)
        return 2

    # FAST-SKIP (optim 11/07, demande Preda) : cookie mort = 401 assure sur
    # toutes les sources utiles -> on ne lit/reecrit PAS l'onglet Pseudos et
    # on ne scrape PAS le leaderboard (discretion + ~2 min de daily gagnees).
    # STACKR_FAST_SKIP=false pour forcer l'ancien comportement (leaderboard).
    if not ok and os.environ.get("STACKR_FAST_SKIP", "true").lower() != "false":
        print("Fast-skip : session verifiedVeve refusee -> aucune collecte. "
              "Renouveler le secret STACKR_COOKIE puis relancer.", flush=True)
        try:
            from scraper.sheets import append_log
            append_log(sheet_id, "pseudos", "SKIPPED",
                       "cookie StackR expire - fast-skip (0 requete)")
        except Exception:
            pass
        return 0

    summary = {"run_at_utc": _now(), "status": "OK", "verified_api": ok}
    try:
        book = PseudoBook(read_existing(sheet_id))
        before = len(book.rows)
        chain_wallets = read_chain_wallets(sheet_id)
        print(f"Pseudos existants: {before} | wallets on-chain candidats: "
              f"{len(chain_wallets)}", flush=True)
        res = harvest(cli, book, chain_wallets)
        write_book(sheet_id, book)
        summary.update({
            "lookups_used": cli.lookups,
            "new_rows": book.new_rows,
            "usernames_found": book.usernames_found,
            "total_rows": len(book.rows),
            "note": res["note"],
        })
        print(f"OK — rows={len(book.rows)} (+{book.new_rows}) "
              f"usernames+={book.usernames_found} lookups={cli.lookups}",
              flush=True)
        print(f"   {res['note']}", flush=True)
    except Exception as e:
        summary.update({"status": "ERROR", "note": f"{type(e).__name__}: {e}"})
        try:
            append_runlog(sheet_id, summary)
        except Exception:
            pass
        raise
    append_runlog(sheet_id, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
