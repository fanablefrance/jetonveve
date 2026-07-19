"""🐋 SUIVI DES COMPTES WHALE / VeVe TEAM — canal Discord dedie.

POURQUOI CE MODULE
------------------
Preda tague certains comptes dans l'onglet 🟣C-PSEUDOS (colonne « Type de
compte » : VeVe Team / Fondateur / Moderation / Publisher / Influenceur / A
suivre). On veut voir CE QUE CES COMPTES FONT, en direct, dans un canal a part.

LES TROIS EVENEMENTS (choix de Preda)
-------------------------------------
  🛒 ACHAT            — un compte suivi ACHETE (flux ventes StackR, buyer).
  💸 VENTE / 🏷️ MISE EN VENTE — il vend ou liste (flux ventes/listings StackR).
  🔀 GROS TRANSFERT   — un mouvement NFT hors marche (wallet-a-wallet) d'au
                        moins WHALE_XFER_MIN jetons dans une meme transaction,
                        lu sur CollectChain (collectscan). On EXCLUT l'escrow du
                        marche (deja couvert par 🛒/💸/🏷️), le mint (from = 0x0)
                        et le burn : ne restent que les vrais transferts entre
                        wallets (cadeau, consolidation, OTC).

LE PONT (comme elements.csv / comics_supply.csv)
------------------------------------------------
Le tag n'existe QUE dans le Sheet, donc chez preda (PRIVE). preda exporte les
lignes taguees dans `data/tracked_accounts.csv` (`export_tracked.py`), jetonveve
le lit en sparse-checkout (`_preda/data/tracked_accounts.csv`). Sans le pont, le
module se tait poliment (aucun compte suivi) — le reste des alertes n'est pas
touche.

ARCHITECTURE
------------
On REUTILISE le moteur de `floor_watch` (memes appels StackR, meme cours OMI,
meme garde-fou anti-ban `budget`/`consommer` a 20 msg/min par webhook) : zero
requete StackR en plus, zero regle de mise en forme dupliquee. Les evenements
marche viennent du flux (2 min = instantane) ; les gros transferts on-chain
sont releves 1x/h (collectscan est un explorateur public, on reste poli).

Anti-bruit : chaque evenement marche est dedoublonne par (genre, nft_id,
timestamp) ; chaque transfert par hash de tx. Au-dela de WHALE_MAX evenements
d'un coup, on ne publie RIEN et on ne MEMORISE RIEN (un seuil mal regle n'est
pas 20 nouvelles ; on crie, l'humain tranche) — meme regle que partout ailleurs.

Construit OFF par defaut (`WHALE_ON`) : on calibre en SIMULER avant d'allumer.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

from scraper import floor_watch as fw

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WHALE_ON = os.environ.get("WHALE_ON", "false").lower() == "true"
# Canal DEDIE. A defaut de secret, on retombe sur le webhook principal (rien
# perdu, mais tout arrive dans le meme salon).
WEBHOOK = (os.environ.get("DISCORD_WEBHOOK_WHALE", "").strip()
           or os.environ.get("DISCORD_WEBHOOK", "").strip())
TRACKED_CSV = os.environ.get("TRACKED_CSV", "_preda/data/tracked_accounts.csv")
STATE_PATH = os.environ.get("WHALE_STATE", "data/whale_state.json")

POLLS = int(os.environ.get("WHALE_POLLS", "25"))
INTERVAL_S = float(os.environ.get("WHALE_INTERVAL_S", "120"))
REFRESH_MIN = float(os.environ.get("WHALE_REFRESH_MIN", "60"))  # transferts 1x/h
MIN_USD = float(os.environ.get("WHALE_MIN_USD", "1"))     # ignore la poussiere
XFER_MIN = int(os.environ.get("WHALE_XFER_MIN", "5"))     # « gros » = >= N jetons/tx
MAX_CARTES = int(os.environ.get("WHALE_MAX", "10"))
VU_TTL = float(os.environ.get("WHALE_VU_TTL_H", "48")) * 3600
XFER_PAGES = int(os.environ.get("WHALE_XFER_PAGES", "3"))
SIMULER = os.environ.get("WHALE_SIMULER", "").strip().lower() in ("1", "oui", "true")

# CollectChain (collectscan) — transferts NFT (ERC-721, verifie le 17/07).
API_BASE = "https://collectscan.com/api/v2"
ZERO = "0x0000000000000000000000000000000000000000"
MARKET_ESCROW = "0xb1af72a77b9065c55cda0680b86655a79b62e42c"
BURN_SINK = "0x39e3816a8c549ec22cd1a34a8cf7034b3941d8b1"
SYSTEM = {ZERO, MARKET_ESCROW, BURN_SINK}
UA = {"User-Agent": "veve-whale-watch/1.0", "Accept": "application/json"}

COULEURS = {"achat": 0x2ECC71, "vente": 0xE67E22, "mise en vente": 0xF1C40F,
            "gros transfert": 0x9B59B6}


def _norm(w) -> str:
    return (w or "").strip().lower()


# ---------------------------------------------------------------------------
# Le pont : les comptes suivis (CSV exporte par preda)
# ---------------------------------------------------------------------------

def charger_tracked(chemin: str = None) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """Lit les lignes 🟣C-PSEUDOS taguees. Renvoie deux index vers la meme
    fiche : par wallet (imx ET stackr, minuscules) et par username (minuscules).
    Une ligne SANS « type » est ignoree (seul le tag fait suivre)."""
    chemin = chemin or TRACKED_CSV
    par_wallet: Dict[str, Dict] = {}
    par_user: Dict[str, Dict] = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                typ = (r.get("type") or r.get("Type de compte") or "").strip()
                if not typ:
                    continue
                user = (r.get("username") or "").strip()
                fiche = {
                    "username": user or "(sans pseudo)",
                    "type": typ,
                    "holdings": (r.get("holdings") or "").strip(),
                    "value_floor": (r.get("value_floor") or "").strip(),
                    "wallet_imx": _norm(r.get("wallet_imx")),
                    "wallet_stackr": _norm(r.get("wallet_stackr")),
                }
                for w in (fiche["wallet_imx"], fiche["wallet_stackr"]):
                    if w:
                        par_wallet[w] = fiche
                if user:
                    par_user[user.lower()] = fiche
    except FileNotFoundError:
        print(f"  (pas de {chemin} : aucun compte suivi — preda ne l'a pas "
              f"encore exporte)", file=sys.stderr)
    return par_wallet, par_user


def _tracke(tracked, wallet, username) -> Optional[Dict]:
    par_wallet, par_user = tracked
    return par_wallet.get(_norm(wallet)) or par_user.get((username or "").strip().lower())


# ---------------------------------------------------------------------------
# 🛒 / 💸 / 🏷️  Evenements de marche (flux StackR, 2 min)
# ---------------------------------------------------------------------------

def detect_marche(state, listings, ventes, tracked, omi):
    """Achats, ventes et mises en vente d'un compte suivi. Chaque evenement est
    identifie par (genre, nft_id, timestamp) et dedoublonne dans `vus`."""
    vus = state.setdefault("whale_vus", {})
    ts = time.time()
    cand: List[Dict] = []
    local = set()

    def _try(genre, emoji, it, fiche, prix_omi):
        nft = str(it.get("nft_id") or "")
        stamp = str(it.get("timestamp") or "")
        cle = genre + "|" + nft + "|" + stamp
        if not nft or cle in vus or cle in local:
            return
        usd = fw._f(prix_omi) * omi if omi else 0.0
        if usd and usd < MIN_USD:
            return
        local.add(cle)
        uid = str(it.get("element_id") or "")
        cat = ("comic" if str(it.get("element_type") or "") == "COMIC_COVER"
               else "collectible")
        cand.append({"cle": cle, "genre": genre, "emoji": emoji,
                     "compte": fiche["username"], "type": fiche["type"],
                     "uuid": uid, "categorie": cat,
                     "name": it.get("name") or uid[:8],
                     "edition": it.get("edition") or "",
                     "usd": round(usd, 2), "omi": round(fw._f(prix_omi))})

    for it in listings or []:
        f = _tracke(tracked, it.get("listed_by"), it.get("listed_by_username"))
        if f:
            _try("mise en vente", "🏷️", it, f, it.get("price"))
    for it in ventes or []:
        fa = _tracke(tracked, it.get("buyer"), it.get("buyer_username"))
        if fa:
            _try("achat", "🛒", it, fa, it.get("price"))
        fv = _tracke(tracked, it.get("listed_by"), it.get("listed_by_username"))
        if fv:
            _try("vente", "💸", it, fv, it.get("price"))

    for k in [k for k, t in list(vus.items()) if ts - fw._f(t) > VU_TTL]:
        vus.pop(k, None)

    if len(cand) > MAX_CARTES:
        print(f"  ⛔ {len(cand)} evenements comptes suivis d'un coup — anormal. "
              f"RIEN publie ni memorise.", file=sys.stderr)
        return []
    for c in cand:
        vus[c["cle"]] = ts
    return cand


# ---------------------------------------------------------------------------
# 🔀  Gros transferts NFT hors marche (collectscan, 1x/h)
# ---------------------------------------------------------------------------

def _get(url, params=None):
    for attempt in range(1, 6):
        try:
            r = requests.get(url, params=params or {}, headers=UA, timeout=40)
            if r.status_code == 429:
                time.sleep(3 * attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            if attempt == 5:
                print(f"    collectscan abandonne : {e}", file=sys.stderr)
                return None
            time.sleep(3 * attempt)
    return None


def detect_transferts(state, tracked):
    """Un mouvement NFT wallet-a-wallet d'un compte suivi, >= WHALE_XFER_MIN
    jetons dans une meme transaction. On exclut l'escrow (marche), le zero
    (mint) et le burn sink : ne restent que les vrais transferts. Dedup par tx.
    """
    par_wallet, _ = tracked
    vus = state.setdefault("whale_tx_vus", {})
    ts = time.time()
    cand: List[Dict] = []

    for wallet, fiche in par_wallet.items():
        if not wallet:
            continue
        url = f"{API_BASE}/addresses/{wallet}/token-transfers"
        params = {"type": "ERC-721"}
        groups: Dict[str, Dict] = {}
        pages = 0
        while pages < XFER_PAGES:
            d = _get(url, params)
            if not d:
                break
            for it in d.get("items") or []:
                tot = it.get("total") or {}
                inst = (tot.get("token_instance") or {}) if isinstance(tot, dict) else {}
                if not inst:
                    continue                       # pas un NFT VeVe (pas d'instance)
                frm = _norm((it.get("from") or {}).get("hash"))
                to = _norm((it.get("to") or {}).get("hash"))
                if wallet not in (frm, to):
                    continue
                autre = to if frm == wallet else frm
                if autre in SYSTEM:                # marche / mint / burn : ecarte
                    continue
                txh = it.get("transaction_hash") or it.get("tx_hash") or ""
                if not txh:
                    continue
                md = inst.get("metadata") or {}
                g = groups.setdefault(txh, {
                    "count": 0,
                    "sens": "sortant" if frm == wallet else "entrant",
                    "autre": autre, "name": (md.get("name") if isinstance(md, dict) else "") or "",
                    "ts": it.get("timestamp") or ""})
                g["count"] += 1
            nxt = d.get("next_page_params")
            if not nxt:
                break
            params = {"type": "ERC-721", **nxt}
            pages += 1
            time.sleep(0.15)

        for txh, g in groups.items():
            if g["count"] < XFER_MIN or txh in vus:
                continue
            cand.append({"genre": "gros transfert", "emoji": "🔀",
                         "compte": fiche["username"], "type": fiche["type"],
                         "txh": txh, "count": g["count"], "sens": g["sens"],
                         "autre": g["autre"], "name": g["name"] or "?"})

    for k in [k for k, t in list(vus.items()) if ts - fw._f(t) > VU_TTL]:
        vus.pop(k, None)

    if len(cand) > MAX_CARTES:
        print(f"  ⛔ {len(cand)} gros transferts d'un coup — anormal. RIEN "
              f"publie ni memorise.", file=sys.stderr)
        return []
    for c in cand:
        vus[c["txh"]] = ts
    return cand


# ---------------------------------------------------------------------------
# Cartes + envoi
# ---------------------------------------------------------------------------

def carte(a):
    tete = "{} {} — {} ({})".format(a["emoji"], a["genre"].capitalize(),
                                    a["compte"], a["type"])
    if a["genre"] == "gros transfert":
        lien = "https://collectscan.com/tx/" + a["txh"]
        desc = ["**{}** jeton(s) {} · contrepartie `{}`".format(
                    a["count"], a["sens"], a["autre"][:10] + "…"),
                a["name"],
                "[Voir la transaction](" + lien + ")"]
    else:
        lien = fw.lien_stackr(a["uuid"], a.get("categorie", ""))
        nom = a["name"] + (" #{}".format(a["edition"]) if a.get("edition") else "")
        prix = "**{:.2f} $** ({} OMI)".format(a["usd"], a["omi"]) if a["usd"] \
            else "prix inconnu"
        desc = [nom, prix, "[Voir sur StackR](" + lien + ")"]
    return {"title": tete[:250], "color": COULEURS.get(a["genre"], 0x95A5A6),
            "description": "\n".join(desc), "url": None if a["genre"] == "gros transfert"
            else fw.lien_stackr(a["uuid"], a.get("categorie", ""))}


def _ligne_sim(a):
    if a["genre"] == "gros transfert":
        return "🔀 {:<20} {} jetons {} <-> {}".format(
            a["compte"][:20], a["count"], a["sens"], a["autre"][:10])
    return "{} {:<20} {:<28} {:>8.2f} $".format(
        a["emoji"], a["compte"][:20], (a["name"][:28]), a["usd"])


def notifier(state, cartes):
    """Un message groupe, 10 cartes max, plafond 20/min par webhook, 429
    respecte — via les garde-fous de floor_watch."""
    if not cartes:
        return 0
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye]", flush=True)
        for a in cartes[:10]:
            print("    " + _ligne_sim(a), flush=True)
        return len(cartes)
    if fw.budget(state, WEBHOOK) <= 0:
        print("  🔇 plafond/min atteint — evenements gardes pour plus tard.",
              flush=True)
        return 0
    contenu = "🐋 **{} evenement(s) — comptes suivis** — {}".format(
        len(cartes), _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC"))
    embeds = [carte(a) for a in cartes[:10]]
    # Les evenements de genre « gros transfert » n'ont pas d'uuid : le pont
    # les ignore tout seul (sans identifiant, impossible de dedoublonner ni
    # de croiser une liste de surveillance).
    from scraper import bot_alertes
    bot_alertes.pousser_lot("whale", cartes[:10], embeds, simuler=SIMULER)
    try:
        r = requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        if r.status_code == 429:
            time.sleep(min(fw._f(r.json().get("retry_after")) + 1, 60))
            requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        fw.consommer(state, WEBHOOK)
        print(f"  Discord : {len(embeds)} carte(s) poussee(s).", flush=True)
    except Exception as e:                                      # noqa: BLE001
        print(f"  Discord KO ({e})", flush=True)
    return len(cartes)


# ---------------------------------------------------------------------------
# Etat + main
# ---------------------------------------------------------------------------

def load_state() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                           # noqa: BLE001
        return {}


def save_state(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def main() -> int:
    t0 = time.time()
    state = load_state()
    tracked = charger_tracked()
    n_comptes = len({id(v) for v in tracked[0].values()} | {id(v) for v in tracked[1].values()})
    print("🐋 suivi comptes whale/team : "
          + ("ON" if WHALE_ON else "OFF (WHALE_ON=true pour l'allumer)")
          + (" · SIMULATION" if SIMULER else "")
          + f" · {n_comptes} compte(s) suivi(s) · canal "
          + ("dedie" if os.environ.get("DISCORD_WEBHOOK_WHALE") else "principal"),
          flush=True)
    if not WHALE_ON:
        print("  (le module est eteint — rien n'est fait)", flush=True)
        return 0
    if not tracked[0] and not tracked[1]:
        print("  aucun compte suivi : rien a faire.", flush=True)
        return 0

    s = requests.Session()
    dernier_refresh = 0.0
    total = 0
    for i in range(1, POLLS + 1):
        omi = fw.fetch_omi_price(s)
        # transferts on-chain : 1x/h (collectscan est public, on reste poli)
        if time.time() - dernier_refresh > REFRESH_MIN * 60:
            dernier_refresh = time.time()
            xfers = detect_transferts(state, tracked)
            if xfers:
                total += notifier(state, xfers)
            print(f"  transferts on-chain releves ({len(xfers)} gros).",
                  flush=True)
        listings = fw.fetch_listings(s)
        ventes = fw.fetch_sales(s)
        evts = detect_marche(state, listings, ventes, tracked, omi)
        if evts:
            print(f"  [{i}/{POLLS}] 🐋 {len(evts)} evenement(s) marche !",
                  flush=True)
            total += notifier(state, evts)
        else:
            print(f"  [{i}/{POLLS}] {len(listings)} listings, {len(ventes)} "
                  f"vente(s) — rien pour un compte suivi.", flush=True)
        save_state(state)
        if i < POLLS:
            time.sleep(INTERVAL_S)
    print(f"Termine : {POLLS} tours, {total} evenement(s), "
          f"{time.time() - t0:.0f}s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
