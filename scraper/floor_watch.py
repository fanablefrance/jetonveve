"""🚨 ALERTES SOUS-FLOOR v3 — le flux des LISTINGS (12/07/2026).

v1/v2 balayaient les 6 011 floors (61 requetes/tour) en esperant voir un floor
s'effondrer. La capture DevTools de Preda a revele BIEN mieux :

  `publicVeve.getAllLatestListings_v2` — PUBLIC, sans cookie : le flux des
  MISES EN VENTE, et chaque ligne porte deja tout ce qu'il faut :
      price (OMI) · nft_id · element_id (= veve_uuid) · edition · name ·
      rarity · timestamp · listed_by (+ username) · **stackr_floor_price**
  ~695 listings par jour -> 1 requete toutes les 2 minutes SUFFIT (au lieu de
  61 par tour). C'est plus reactif ET beaucoup plus discret.

  ATTENTION : l'input DOIT contenir le bloc `meta` (les champs null sont
  encodes en "undefined" par superjson) — un input allege renvoie du vide.

ATTENTION AU SENS DES COLONNES (correction de Preda, capture a l'appui) :
  `price` = ce que DEMANDE le vendeur de CE listing ;
  `stackr_floor_price` = l'offre la MOINS CHERE du marche StackR pour cet item
  (celle de quelqu'un d'autre). Ex. reel : Starlight Orb liste a 295 000 OMI
  (50,15 $) alors que le floor StackR est a 8 000 OMI (1,36 $).

TROIS SIGNAUX, DEUX MARCHES (tout verifie le 12/07) :
  1. LISTING sous le floor StackR : price < stackr_floor_price (les deux en
     OMI) -> quelqu'un vient de brader.
  2. LISTING sous le floor VeVe : price converti en $ (via getTokenPrices ->
     omiPrice) < floor VeVe (getElements, en gems ~ $).
  3. ECART ENTRE MARCHES (ajoute apres la remarque de Preda) : meme sans
     nouveau listing, une offre DEJA EN PLACE peut etre bien moins chere sur un
     marche que sur l'autre. Ex. reel : Starlight Orb, floor StackR 8 000 OMI =
     1,36 $ contre un floor VeVe de 6,59 $. On compare donc aussi les DEUX
     FLOORS entre eux (le floor StackR est memorise au fil des listings vus).
     C'est l'arbitrage : acheter au floor StackR, revendre au floor VeVe.

Conforme a la regle des collecteurs longs : etat persistant, jamais de perte,
backoff, et auto-controle de la pagination (`page`, pas `cursor` — verifie).

Env : DISCORD_WEBHOOK (sinon simulation dans les logs), FLOOR_DROP_PCT (10),
      FLOOR_POLLS (25), FLOOR_INTERVAL_S (120), FLOOR_LISTINGS (50),
      FLOOR_REFRESH_MIN (60 = rafraichissement des floors VeVe),
      FLOOR_MIN_USD (1 = ignore les broutilles), FLOOR_COOLDOWN_H (6),
      FLOOR_STATE (data/floor_state.json).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

BASE = "https://www.stackr.world/api/trpc/publicVeve."
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

STATE_PATH = os.environ.get("FLOOR_STATE", "data/floor_state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DROP_PCT = float(os.environ.get("FLOOR_DROP_PCT", "10"))
POLLS = int(os.environ.get("FLOOR_POLLS", "25"))
INTERVAL_S = int(os.environ.get("FLOOR_INTERVAL_S", "120"))
N_LISTINGS = int(os.environ.get("FLOOR_LISTINGS", "50"))
REFRESH_MIN = float(os.environ.get("FLOOR_REFRESH_MIN", "60"))
MIN_USD = float(os.environ.get("FLOOR_MIN_USD", "1"))
SPREAD_PCT = float(os.environ.get("FLOOR_SPREAD_PCT", "40"))
# TROIS SEUILS DISTINCTS (1er run reel, 12/07 : 15 alertes sur 50 listings avec
# un seuil unique a 10 % !). La lecture des alertes est sans appel : les offres
# StackR sont STRUCTURELLEMENT moins cheres que le floor VeVe (marche moins
# liquide, prix en OMI). Comparer un prix StackR au floor VeVe a 10 % revient
# donc a alerter en permanence. On exige beaucoup plus pour cette comparaison
# inter-marches, et on garde un seuil bas pour la seule qui soit vraiment
# "toutes choses egales par ailleurs" : prix StackR vs floor StackR.
VEVE_PCT = float(os.environ.get("FLOOR_VEVE_PCT", "35"))
# ARBITRAGE VERIFIE A LA MAIN PAR PREDA (12/07) : Donatello - IncogNinja, offre
# StackR a 21,72 $, floor VeVe reellement a 30,00 $ (verifie dans l'app). Le
# signal est donc REEL. Mais une decote brute ne dit pas si l'affaire est
# bonne : ce qui compte, c'est ce qui RESTE APRES LES FRAIS. On raisonne donc
# en MARGE NETTE :
#     benefice = floor_VeVe x (1 - frais) - prix_d_achat
# Frais VeVe ~8,5 % (2,5 % VeVe + fee licensor — cf. marketFee du catalogue).
# NUANCE HONNETE : le floor VeVe est un prix DEMANDE, pas un acheteur qui
# attend. Revendre suppose de se placer sous ce floor et d'attendre preneur —
# la marge affichee est un PLAFOND, pas un gain garanti.
FEE_PCT = float(os.environ.get("FLOOR_FEE_PCT", "8.5"))
MARGIN_PCT = float(os.environ.get("FLOOR_MARGIN_PCT", "20"))
MIN_PROFIT = float(os.environ.get("FLOOR_MIN_PROFIT", "5"))


def _marge(achat_usd: float, floor_veve_usd: float):
    """(benefice net en $, marge en % du capital engage)."""
    if achat_usd <= 0 or floor_veve_usd <= 0:
        return 0.0, 0.0
    net = floor_veve_usd * (1.0 - FEE_PCT / 100.0) - achat_usd
    return net, 100.0 * net / achat_usd
COOLDOWN_H = float(os.environ.get("FLOOR_COOLDOWN_H", "6"))
ELEM_LIMIT = int(os.environ.get("FLOOR_ELEM_LIMIT", "100"))
RETRIES = int(os.environ.get("FLOOR_RETRIES", "6"))
TIMEOUT = int(os.environ.get("FLOOR_TIMEOUT", "45"))
PAUSE = float(os.environ.get("FLOOR_PAUSE", "0.2"))


def _get(proc: str, payload: Optional[Dict], session=None, meta=None):
    """Appel trpc. None = echec definitif (jamais d'exception qui tue le run)."""
    inp: Dict = {"json": payload}
    if meta:
        inp["meta"] = meta
    url = BASE + proc + "?input=" + urllib.parse.quote(
        json.dumps(inp, separators=(",", ":")))
    s = session or requests
    for attempt in range(RETRIES):
        try:
            r = s.get(url, headers={"User-Agent": UA,
                                    "Accept": "application/json"},
                      timeout=TIMEOUT)
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return (r.json().get("result", {}).get("data", {})
                    .get("json"))
        except Exception as e:
            if attempt == RETRIES - 1:
                print(f"    {proc} abandonne : {e}", flush=True)
                return None
            wait = min(60, 3 * (2 ** attempt))
            print(f"    {proc} : {e} — nouvel essai dans {wait} s", flush=True)
            time.sleep(wait)
    return None


def _f(x) -> float:
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def fetch_listings(session=None, limit: int = N_LISTINGS) -> List[Dict]:
    """Les derniers listings (le bloc `meta` est OBLIGATOIRE)."""
    d = _get("getAllLatestListings_v2",
             {"limit": str(limit), "elementType": None, "rarity": None,
              "edition": None, "sortBy": "timestamp",
              "sortDirection": "desc", "timeframe": "1d",
              "direction": "forward"},
             session,
             meta={"values": {"elementType": ["undefined"],
                              "rarity": ["undefined"],
                              "edition": ["undefined"]}, "v": 1})
    if not d:
        return []
    return d.get("items") or []


def fetch_omi_price(session=None) -> float:
    d = _get("getTokenPrices", None, session,
             meta={"values": ["undefined"], "v": 1})
    return _f((d or {}).get("omiPrice"))


def fetch_veve_floors(session=None) -> Dict[str, float]:
    """{uuid -> floor VeVe (gems ~ $)} via getElements.

    PAGINATION : le parametre est `page` (1-based). `cursor`/`offset`/`skip`
    sont IGNORES EN SILENCE (verifie le 12/07) -> auto-controle : si la page 2
    renvoie la page 1, on abandonne au lieu de croire qu'on a tout."""
    out: Dict[str, float] = {}
    page, total, tete = 1, None, None
    while page <= 200:
        d = _get("getElements", {"limit": ELEM_LIMIT, "page": page}
                 if page > 1 else {"limit": ELEM_LIMIT}, session)
        if d is None:
            page += 1
            continue
        rows = d.get("data") or []
        if not rows:
            break
        if total is None:
            total = int(d.get("totalCount") or 0)
        prem = str(rows[0].get("id") or "")
        if page == 1:
            tete = prem
        elif prem == tete:
            print("  !! PAGINATION getElements CASSEE (page 2 == page 1) — "
                  "floors VeVe ignores ce tour.", flush=True)
            return {}
        for e in rows:
            uid = str(e.get("id") or "")
            if uid:
                out[uid] = _f(e.get("floor_market_price"))
        if total and len(out) >= total:
            break
        page += 1
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect(state: Dict, listings: List[Dict], omi: float,
           veve: Dict[str, float], ts: float = None) -> List[Dict]:
    """Un listing sous le floor = une affaire. Deux comparaisons :
      * marche StackR : price vs stackr_floor_price (tous deux en OMI) ;
      * marche VeVe   : price converti en $ vs floor VeVe (gems ~ $).
    Anti-bruit : listing deja vu, montant derisoire, cooldown par item."""
    ts = ts if ts is not None else time.time()
    vus: Dict[str, float] = state.setdefault("vus", {})
    alerts: Dict[str, float] = state.setdefault("alerts", {})
    out: List[Dict] = []
    for it in listings:
        nft = str(it.get("nft_id") or "")
        stamp = str(it.get("timestamp") or "")
        cle = nft + "|" + stamp
        if not nft or cle in vus:
            continue
        vus[cle] = ts
        price = _f(it.get("price"))
        if price <= 0:
            continue
        usd = price * omi if omi else 0.0
        if usd and usd < MIN_USD:
            continue                       # broutille : on ignore
        sf = _f(it.get("stackr_floor_price"))
        uid = str(it.get("element_id") or "")
        if uid and sf > 0:
            # on memorise le floor StackR de l'item (pour le signal 3)
            state.setdefault("sfloors", {})[uid] = [
                sf, it.get("name") or uid[:8], it.get("rarity") or ""]
        vf = veve.get(uid, 0.0)
        d_stackr = (100.0 * (sf - price) / sf) if sf > 0 else 0.0
        d_veve = (100.0 * (vf - usd) / vf) if (vf > 0 and usd > 0) else 0.0
        net, marge = _marge(usd, vf)
        # Deux regles :
        #  * MEME MARCHE : le listing passe sous le floor StackR (sous-cotation
        #    franche) -> seuil en % suffisant ;
        #  * AUTRE MARCHE : on ne retient que ce qui RAPPORTE VRAIMENT une fois
        #    les frais VeVe payes (marge nette ET benefice minimum en $).
        arbitrage = (marge >= MARGIN_PCT and net >= MIN_PROFIT)
        if d_stackr < DROP_PCT and not arbitrage:
            continue
        if ts - alerts.get(uid or nft, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid or nft] = ts
        out.append({"net": round(net, 2), "marge": round(marge, 1),
                    "nft": nft, "uuid": uid,
                    "name": it.get("name") or uid[:8],
                    "rarity": it.get("rarity") or "",
                    "edition": it.get("edition"),
                    "price": price, "usd": usd,
                    "stackr_floor": sf, "veve_floor": vf,
                    "d_stackr": round(d_stackr, 1),
                    "d_veve": round(d_veve, 1),
                    "seller": it.get("listed_by_username")
                    or (it.get("listed_by") or "")[:10]})
    # menage de l'etat (on ne garde que 24 h de listings vus)
    for k, t in list(vus.items()):
        if ts - t > 86400:
            vus.pop(k, None)
    out.sort(key=lambda a: -a.get("net", 0))   # par BENEFICE, pas par %
    return out


def detect_spread(state: Dict, veve: Dict[str, float], omi: float,
                  ts: float = None) -> List[Dict]:
    """SIGNAL 3 — ecart entre marches, sur les offres DEJA EN PLACE.

    Le floor StackR de chaque item est memorise au fil des listings vus
    (`sfloors` dans l'etat). Si le floor StackR converti en $ est nettement
    sous le floor VeVe, il y a une offre achetable tout de suite ici et
    revendable la-bas."""
    ts = ts if ts is not None else time.time()
    sfloors: Dict[str, list] = state.setdefault("sfloors", {})
    alerts: Dict[str, float] = state.setdefault("alerts", {})
    out: List[Dict] = []
    if not omi:
        return out
    for uid, (sf, name, rarity) in list(sfloors.items()):
        vf = veve.get(uid, 0.0)
        sf_usd = sf * omi
        if vf <= 0 or sf_usd <= 0 or sf_usd < MIN_USD:
            continue
        ecart = 100.0 * (vf - sf_usd) / vf
        net, marge = _marge(sf_usd, vf)
        if ecart < SPREAD_PCT or marge < MARGIN_PCT or net < MIN_PROFIT:
            continue
        # MEME verrou que les listings (cle = uuid) : un item deja signale
        # comme listing ne doit PAS ressortir en "ecart de marches" — c'est la
        # MEME affaire vue sous deux angles (constate au run du 12/07 :
        # Fantastic Four et Ryu Battle alertaient deux fois).
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        out.append({"net": round(net, 2), "marge": round(marge, 1),
                    "nft": "", "uuid": uid, "name": name, "rarity": rarity,
                    "edition": "", "price": sf, "usd": sf_usd,
                    "stackr_floor": sf, "veve_floor": vf,
                    "d_stackr": 0.0, "d_veve": round(ecart, 1),
                    "seller": "(floor du marche)", "spread": True})
    out.sort(key=lambda a: -a.get("net", 0))
    return out


def notify(alerts: List[Dict]) -> int:
    if not alerts:
        return 0
    lignes = []
    for a in alerts[:8]:
        if a.get("spread"):
            lignes.append(
                (f"↔️ **{a['name']}** ({a['rarity']}) — écart entre marchés\n"
                 f"acheter au floor StackR **{a['usd']:,.2f} $** · floor VeVe "
                 f"**{a['veve_floor']:,.2f} $** · frais {FEE_PCT} %\n"
                 f"→ **+{a['net']:,.2f} $ net (+{a['marge']} %)**\n"
                 f"<https://www.stackr.world/element/{a['uuid']}>")
                .replace(",", " "))
            continue
        parts = []
        if a["d_stackr"] >= DROP_PCT:
            parts.append(f"**−{a['d_stackr']} % sous le floor StackR** "
                         f"({a['stackr_floor']:,.0f} OMI)")
        if a["d_veve"] >= VEVE_PCT:
            parts.append(f"−{a['d_veve']} % / floor VeVe "
                         f"({a['veve_floor']:,.2f} $)")
        if a.get("net", 0) >= MIN_PROFIT and a.get("marge", 0) >= MARGIN_PCT:
            parts.append(f"revente au floor VeVe {a['veve_floor']:,.2f} $ → "
                         f"**+{a['net']:,.2f} $ net (+{a['marge']} %)** "
                         f"apres {FEE_PCT} % de frais")
        lignes.append(
            (f"**{a['name']}** #{a['edition']} ({a['rarity']})\n"
             f"achat **{a['price']:,.0f} OMI** (~{a['usd']:,.2f} $) — "
             + " · ".join(parts) +
             f"\npar {a['seller']} · <https://www.stackr.world/element/"
             f"{a['uuid']}>").replace(",", " "))
    corps = ("🚨 **AFFAIRES** — " +
             _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC") +
             "\n\n" + "\n\n".join(lignes))
    if len(alerts) > 8:
        corps += f"\n\n… et {len(alerts) - 8} autre(s)."
    corps += ("\n\n*Le floor VeVe est un prix DEMANDÉ : revendre suppose de "
              "se placer dessous et d'attendre preneur. La marge est un "
              "plafond, pas un gain garanti.*")
    if not WEBHOOK:
        print("  [SIMULATION — pas de DISCORD_WEBHOOK]\n" + corps, flush=True)
        return len(alerts)
    try:
        requests.post(WEBHOOK, json={"content": corps[:1900]},
                      timeout=20).raise_for_status()
        print(f"  Discord : {len(alerts)} alerte(s).", flush=True)
    except Exception as e:
        print(f"  Discord KO ({e}) :\n{corps}", flush=True)
    return len(alerts)


def load_state() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"vus": {}, "alerts": {}}


def save_state(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def main() -> int:
    t0 = time.time()
    state = load_state()
    s = requests.Session()
    veve: Dict[str, float] = {}
    dernier_refresh = 0.0
    total = 0
    for i in range(1, POLLS + 1):
        # floors VeVe : rafraichis 1x/heure (61 requetes) — pas a chaque tour
        if time.time() - dernier_refresh > REFRESH_MIN * 60:
            neuf = fetch_veve_floors(s)
            if neuf:
                veve = neuf
                dernier_refresh = time.time()
                print(f"  floors VeVe rafraichis : {len(veve)} elements.",
                      flush=True)
        omi = fetch_omi_price(s)
        listings = fetch_listings(s)
        if not listings:
            print(f"  [{i}/{POLLS}] aucun listing recu — on reessaiera.",
                  flush=True)
        else:
            a = detect(state, listings, omi, veve)
            a += detect_spread(state, veve, omi)     # signal 3
            print(f"  [{i}/{POLLS}] {len(listings)} listings, "
                  f"{len(a)} alerte(s), OMI={omi:.6f} $", flush=True)
            total += notify(a)
            save_state(state)
        if i < POLLS:
            time.sleep(INTERVAL_S)
    print(f"Termine : {POLLS} tours, {total} alerte(s), "
          f"{time.time() - t0:.0f}s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN floor_watch.py v7 (une seule alerte par item, triee par benefice)
