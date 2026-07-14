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
# HISTORIQUE DES VENTES (1er run reel : TOUTES les alertes disaient « aucune
# vente recente vue » — getAllLatestSales_v2 ne montre que ~50 ventes du jour).
# On reconstruit donc la derniere vente de chaque element depuis le flux COMPLET
# getVeveTransactions (celui du backfill : il pagine loin, 100 tx/page).
# FLOOR_SALES_PAGES=120 -> ~12 000 tx -> ~7 jours de ventes, 1 fois par run.
SALES_PAGES = int(os.environ.get("FLOOR_SALES_PAGES", "120"))
SALES_TYPES = ("MARKET_FIXED", "MARKET_AUCTION", "MARKET_STACKR")
# Un item SANS AUCUNE VENTE dans cette fenetre est illiquide : « revendre au
# floor VeVe » y est une FICTION. Par defaut on n'alerte donc pas sur l'ecart
# entre marches sans preuve de vente (FLOOR_REQUIRE_SALE=false pour desactiver).
REQUIRE_SALE = os.environ.get("FLOOR_REQUIRE_SALE", "true").lower() != "false"

# ⚠️ MODE REGLAGE. Desserrer un seuil et decouvrir le resultat SUR LE DISCORD DE
# LA COMMU, c'est se tromper devant tout le monde. FLOOR_SIMULER=1 : on calcule
# tout, on n'envoie RIEN, on ecrit dans les logs.
SIMULER = os.environ.get("FLOOR_SIMULER", "").lower() in ("1", "oui", "true")

# ═══════════════════════════════════════════════════════════════════════════
# 📚 SIGNAL 4 — LE COMIC A PETIT TIRAGE, BRADE (demande de Preda, 14/07)
# ═══════════════════════════════════════════════════════════════════════════
# « Un comic a 1 000 exemplaires liste sous 2 $, sur VeVe ou sur StackR. »
#
# Ce n'est PAS un arbitrage, et c'est ce qui change tout :
#   * on ne compare rien, on ne promet aucune revente : on constate un PRIX
#     D'ENTREE absurde au regard de la rarete ;
#   * donc **pas de preuve de vente exigee**. Le garde-fou « 71 % des items ne
#     se vendent jamais » protege les promesses de PLUS-VALUE. Ici il n'y en a
#     pas : a 2 $ pour 1/1000, le risque est de 2 $. Exiger une vente recente
#     ferait taire exactement les items les plus dormants — c'est-a-dire
#     precisement ceux qu'on cherche.
#
# Le TIRAGE ne vient pas de StackR (son champ `quantity` n'est pas prouve) mais
# du CSV exporte par preda depuis le Sheet (`export_comics.py`), recupere ici en
# lecture seule. ⚠️ Le supply d'un comic est celui de la SERIE, pas la somme de
# ses raretes (piege paye le 14/07 sur Captain America #7).
COMICS_CSV = os.environ.get("COMICS_CSV", "_preda/data/comics_supply.csv")
COMIC_SUPPLY_MAX = int(os.environ.get("COMIC_SUPPLY_MAX", "1000"))
COMIC_MAX_USD = float(os.environ.get("COMIC_MAX_USD", "2"))
# GARDE-FOU ANTI-DELUGE : si le 1er passage trouve des dizaines de comics sous
# le seuil, c'est le SEUIL qui est mal regle — on ne noie pas la commu sous
# 200 cartes. On ne publie RIEN, on ne memorise RIEN (sinon on les enterrerait
# pour de bon), on CRIE, et l'humain tranche.
COMIC_MAX_ALERTES = int(os.environ.get("COMIC_MAX_ALERTES", "25"))


def charger_comics(chemin: str = None) -> Dict[str, Dict]:
    """{uuid -> {name, rarity, edition, supply, serie}} pour les petits tirages."""
    import csv as _csv
    chemin = chemin or COMICS_CSV
    out: Dict[str, Dict] = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                uid = (r.get("veve_uuid") or "").strip()
                sup = int(_f(r.get("supply")))
                if not uid or not sup or sup > COMIC_SUPPLY_MAX:
                    continue
                out[uid] = {"name": r.get("name") or uid[:8],
                            "rarity": r.get("rarity") or "",
                            "edition": r.get("edition_type") or "",
                            "supply": sup,
                            "serie": (r.get("series_uuid") or "").strip()}
    except FileNotFoundError:
        print(f"  (pas de {chemin} : signal comics desactive)", file=sys.stderr)
    return out


def detect_comics(state: Dict, comics: Dict[str, Dict],
                  veve: Dict[str, float], listings: List[Dict], omi: float,
                  ts: float = None) -> List[Dict]:
    """Un comic a petit tirage sous COMIC_MAX_USD, des DEUX cotes du marche."""
    ts = ts if ts is not None else time.time()
    if not comics:
        return []
    vus: Dict[str, float] = state.setdefault("comics_vus", {})
    trouve: Dict[str, Dict] = {}

    def _garder(uid, prix, ou):
        c = comics[uid]
        anc = trouve.get(uid)
        if anc and anc["usd"] <= prix:
            return
        trouve[uid] = {"uuid": uid, "usd": round(prix, 2), "ou": ou,
                       "name": c["name"], "rarity": c["rarity"],
                       "edition": c["edition"], "supply": c["supply"],
                       "serie": c["serie"],
                       "veve_floor": veve.get(uid, 0.0)}

    # 1. VEVE — le floor du marche VeVe (gems ~ $), rafraichi 1x/h
    for uid, vf in (veve or {}).items():
        if uid in comics and 0 < vf < COMIC_MAX_USD:
            _garder(uid, vf, "VeVe")

    # 2. STACKR — les listings du flux (prix en OMI)
    for it in listings or []:
        uid = str(it.get("element_id") or "")
        if uid not in comics or not omi:
            continue
        usd = _f(it.get("price")) * omi
        if 0 < usd < COMIC_MAX_USD:
            _garder(uid, usd, "StackR")

    # 3. STACKR — les floors deja connus des items vus (offres EN PLACE)
    for uid, infos in (state.get("sfloors") or {}).items():
        if uid not in comics or not omi:
            continue
        usd = _f(infos[0]) * omi
        if 0 < usd < COMIC_MAX_USD:
            _garder(uid, usd, "StackR")

    out = [a for uid, a in trouve.items()
           if ts - vus.get(uid, 0) >= COOLDOWN_H * 3600]
    if not out:
        return []
    if len(out) > COMIC_MAX_ALERTES:
        # ⚠️ ON NE MEMORISE RIEN : un garde-fou ne doit jamais detruire ce qu'il
        # protege. Si on notait ces uuid comme « vus », ils seraient enterres
        # pour 6 h et Preda ne les reverrait jamais.
        print(f"  ⛔ {len(out)} comics sous {COMIC_MAX_USD:g} $ — c'est le SEUIL "
              f"qui est trop large, pas une aubaine. RIEN n'est publie ni "
              f"memorise. Baisse COMIC_MAX_USD ou COMIC_SUPPLY_MAX, ou releve "
              f"COMIC_MAX_ALERTES si tu les veux vraiment tous.",
              file=sys.stderr)
        for a in sorted(out, key=lambda x: x["usd"])[:10]:
            print(f"     {a['name'][:40]:<40} {a['usd']:>6.2f} $ · "
                  f"{a['supply']} ex. · {a['ou']}", file=sys.stderr)
        return []
    for a in out:
        vus[a["uuid"]] = ts
    out.sort(key=lambda a: (a["supply"], a["usd"]))   # le plus rare d'abord
    return out


# ⚠️ LE LIEN VEVE D'UN COMIC : la page marche d'une RARETE, donc l'uuid de
# l'ELEMENT — PAS celui de la serie. (Deja paye le 14/07 sur les crafts :
# confondre les deux uuid donne la page de QUELQU'UN D'AUTRE, et un lien qui
# s'ouvre a l'air juste tout en etant faux.)
LIEN_COMIC = "https://www.veve.me/collectibles/en/market/comics/{uuid}"


def carte_comic(a: Dict) -> Dict:
    lien = LIEN_COMIC.format(uuid=a["uuid"]) if a.get("uuid") else ""
    lignes = [f"**Tirage** : {a['supply']:,} exemplaires".replace(",", " "),
              f"**Prix** : **{a['usd']:.2f} $** sur **{a['ou']}**"]
    if a.get("veve_floor"):
        lignes.append(f"Floor VeVe : {a['veve_floor']:.2f} $")
    if a.get("rarity") or a.get("edition"):
        lignes.append(f"{a.get('rarity', '')} {a.get('edition', '')}".strip())
    if lien:
        lignes.append(f"[Voir sur VeVe]({lien})")
    return {"title": f"📚 {a['name']}"[:250],
            "description": "\n".join(lignes),
            "color": 0x9B59B6,
            "url": lien or None}


def notify_comics(alerts: List[Dict]) -> int:
    if not alerts:
        return 0
    contenu = (f"📚 **{len(alerts)} comic(s) a petit tirage sous "
               f"{COMIC_MAX_USD:g} $** — "
               + _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC"))
    embeds = [carte_comic(a) for a in alerts[:10]]
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye]", flush=True)
        for a in alerts[:10]:
            print(f"    📚 {a['name'][:40]:<40} {a['usd']:>6.2f} $ · "
                  f"{a['supply']} ex. · {a['ou']}", flush=True)
        return len(alerts)
    try:
        r = requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        if r.status_code == 429:
            time.sleep(min(_f(r.json().get("retry_after")) + 1, 60))
            requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        print(f"  Discord : {len(alerts)} comic(s) pousse(s).", flush=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"  Discord KO ({e})", flush=True)
    return len(alerts)



class Journal:
    """⚠️ UN GARDE-FOU QUI NE DIT PAS POURQUOI IL BLOQUE EST UN MUR.

    Zero alerte pendant deux jours, et aucun moyen de savoir QUI serrait trop :
    la marge ? le benefice minimum ? l'ecart ? la preuve de vente ? Desserrer au
    hasard, c'est deviner — et si deux verrous cedent en meme temps, on ne saura
    toujours pas lequel bloquait. Alors on MESURE : chaque recale est compte par
    motif, et ceux qui ont manque de peu sont gardes AVEC LEURS CHIFFRES.

    Un run, et on sait exactement quel cran tourner, et de combien."""

    LIBELLES = [
        ("broutille", "trop petits (montant derisoire)"),
        ("sans_floor", "sans floor VeVe connu (invendable en face)"),
        ("illiquide", "ILLIQUIDES : aucune vente reelle vue"),
        ("marge", "marge nette insuffisante"),
        ("profit", "benefice en $ insuffisant"),
        ("sous_cote", "pas assez sous le floor StackR"),
        ("ecart", "ecart entre marches insuffisant"),
        ("cooldown", "deja alerte recemment"),
    ]
    SEUILS = {"marge": ("FLOOR_MARGIN_PCT", MARGIN_PCT, "pts de marge"),
              "profit": ("FLOOR_MIN_PROFIT", MIN_PROFIT, "$ de benefice"),
              "ecart": ("FLOOR_SPREAD_PCT", SPREAD_PCT, "pts d'ecart"),
              "sous_cote": ("FLOOR_DROP_PCT", DROP_PCT, "pts sous le floor"),
              "illiquide": ("FLOOR_SALES_PAGES", SALES_PAGES,
                            "pages d'historique de ventes")}

    def __init__(self):
        self.listings = 0
        self.items = set()
        self.motifs: Dict[str, int] = {}
        self.presque: List[Dict] = []
        self._deja = set()

    def rejet(self, motif: str, cand: Dict = None, cle: str = None) -> None:
        if cle is not None:
            if (motif, cle) in self._deja:
                return          # le meme item recale a chaque tour ne compte qu'une fois
            self._deja.add((motif, cle))
        self.motifs[motif] = self.motifs.get(motif, 0) + 1
        if cand and cand.get("net", 0) > 0:
            self.presque.append(dict(cand, bloque_par=motif))

    def _manque(self, c: Dict) -> str:
        m = c.get("bloque_par")
        if m == "marge":
            return f"il manque {MARGIN_PCT - c['marge']:.1f} pts de marge"
        if m == "profit":
            return f"il manque {MIN_PROFIT - c['net']:.2f} $ de benefice"
        if m == "ecart":
            return f"il manque {SPREAD_PCT - c.get('ecart', 0):.1f} pts d'ecart"
        if m == "illiquide":
            return "AUCUNE VENTE connue — ce n'est pas un seuil, c'est la preuve qui manque"
        if m == "sous_cote":
            return f"il manque {DROP_PCT - c.get('d_stackr', 0):.1f} pts sous le floor StackR"
        return m or ""

    def resume(self) -> str:
        l = ["", "═" * 66,
             "  JOURNAL DES RECALES — pourquoi rien n'est sorti",
             "═" * 66,
             f"  Examines : {self.listings} listing(s) neuf(s) · "
             f"{len(self.items)} item(s) du marche"]
        if not self.motifs:
            l.append("  Aucun candidat ecarte : il n'y avait RIEN a examiner. "
                     "Ce n'est pas un probleme de seuil — c'est la source.")
            return "\n".join(l + ["═" * 66, ""])
        l.append("  Ecartes :")
        for k, lib in self.LIBELLES:
            n = self.motifs.get(k, 0)
            if not n:
                continue
            s = self.SEUILS.get(k)
            reglage = f"   [{s[0]}={s[1]:g}]" if s else ""
            l.append(f"     {n:>5}  {lib}{reglage}")
        # LE VERDICT : quel verrou ecarte le plus de candidats REELS ?
        vrais = {k: v for k, v in self.motifs.items()
                 if k in self.SEUILS and v}
        if vrais:
            pire = max(vrais, key=lambda k: vrais[k])
            s = self.SEUILS[pire]
            l += ["", f"  ➜ LE VERROU QUI BLOQUE LE PLUS : {s[0]} (={s[1]:g}) — "
                      f"{vrais[pire]} candidat(s) ecarte(s) par lui seul."]
            if pire == "illiquide":
                l.append("    ⚠️ Ce n'est PAS un seuil de rentabilite : ces items "
                         "ne se vendent pas (ou pas dans notre fenetre de ~7 j).")
                l.append("    Elargir FLOOR_SALES_PAGES donnerait plus de preuves ; "
                         "baisser les marges ne servirait a RIEN.")
        if self.presque:
            self.presque.sort(key=lambda c: -c.get("net", 0))
            l += ["", "  CEUX QUI ONT MANQUE DE PEU (par benefice) :"]
            for c in self.presque[:8]:
                l.append(f"     {c.get('name', '?')[:34]:<34} achat "
                         f"{c.get('usd', 0):>8,.2f} $ → revente "
                         f"{c.get('ref', 0):>9,.2f} $ · marge "
                         f"{c.get('marge', 0):>6.1f} % · net "
                         f"{c.get('net', 0):>+9,.2f} $")
                l.append(f"     {'':34} ↳ {self._manque(c)}")
        l += ["═" * 66, ""]
        return "\n".join(l)


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


def fetch_sales(session=None, limit: int = 50) -> List[Dict]:
    """Les dernieres VENTES REELLES (publicVeve.getAllLatestSales_v2).

    Pourquoi : le floor est un prix DEMANDE. Ce qui prouve qu'un item vaut son
    prix, c'est que quelqu'un l'a PAYE. Verification de Preda (12/07) sur
    Fantastic Four #1 SR : ventes reelles a 2 200 $ (07/07), 2 500 $ (02/07) et
    3 975 $ (13/03) — pendant qu'une offre trainait a 676 $. L'affaire etait
    donc bien reelle, et c'est la VENTE qui le prouve, pas le floor.

    Deux formes d'input essayees (l'endpoint exige le bloc meta superjson ;
    la forme exacte varie selon les champs envoyes)."""
    formes = [
        ({"limit": str(limit), "elementType": None, "rarity": None,
          "edition": None, "sortBy": "timestamp", "sortDirection": "desc",
          "timeframe": "1d", "direction": "forward"},
         {"values": {"elementType": ["undefined"], "rarity": ["undefined"],
                     "edition": ["undefined"]}, "v": 1}),
        ({"limit": str(limit), "timeframe": "1d", "direction": "forward"},
         {"values": {}, "v": 1}),
    ]
    for payload, meta in formes:
        d = _get("getAllLatestSales_v2", payload, session, meta=meta)
        items = (d or {}).get("items") if isinstance(d, dict) else None
        if items:
            return items
    print("    (ventes indisponibles ce tour — alertes basees sur le seul "
          "floor)", flush=True)
    return []


def fetch_history(session=None, pages: int = SALES_PAGES,
                  omi: float = 0.0) -> Dict[str, list]:
    """{uuid -> [prix $ de la DERNIERE vente, jour]} depuis getVeveTransactions.

    Ce flux porte element_id + price + created_at pour CHAQUE vente
    (MARKET_FIXED / MARKET_AUCTION / MARKET_STACKR) et pagine loin (`cursor` =
    numero de page) — contrairement au flux des ventes StackR, limite a ~50
    lignes du jour, qui laissait les alertes sans preuve."""
    out: Dict[str, list] = {}
    s = session or requests.Session()
    for page in range(1, pages + 1):
        payload = {"limit": 100}
        if page > 1:
            payload["cursor"] = page
            payload["direction"] = "forward"
        d = _get("getVeveTransactions", payload, s)
        if not d:
            break
        for it in d:
            if str(it.get("veve_type")) not in SALES_TYPES:
                continue
            if str(it.get("status")) != "COMPLETE":
                continue
            uid = str(it.get("element_id") or "")
            pr = _f(it.get("price"))
            if not uid or pr <= 0 or uid in out:
                continue                  # 1re occurrence = la plus RECENTE
            out[uid] = [round(pr * omi, 2) if omi else 0.0,
                        str(it.get("created_at") or "")[:10]]
        time.sleep(PAUSE)
    return out


def merge_history(state: Dict, hist: Dict[str, list]) -> int:
    """Injecte l'historique dans l'etat (meme format que note_sales)."""
    m: Dict[str, list] = state.setdefault("sales", {})
    n = 0
    for uid, (usd, jour) in hist.items():
        if usd and uid not in m:
            m[uid] = [usd, time.time(), jour]
            n += 1
    return n


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

def note_sales(state: Dict, sales: List[Dict], omi: float,
               ts: float = None) -> int:
    """Memorise la DERNIERE VENTE REELLE de chaque element (prix en $)."""
    ts = ts if ts is not None else time.time()
    m: Dict[str, list] = state.setdefault("sales", {})
    n = 0
    for s_ in sales or []:
        uid = str(s_.get("element_id") or "")
        pr = _f(s_.get("price"))
        if not uid or pr <= 0:
            continue
        usd = pr * omi if omi else 0.0
        anc = m.get(uid)
        # on garde la vente la plus RECENTE
        if not anc or str(s_.get("timestamp") or "") >= str(anc[2] or ""):
            m[uid] = [round(usd, 2), ts, str(s_.get("timestamp") or "")]
            n += 1
    return n


def _revente(vf: float, uid: str, state: Dict):
    """Prix de revente RETENU + derniere vente connue.

    Prudence : si l'item s'est vendu MOINS cher que le floor affiche, c'est ce
    prix-la qui fait foi (le floor n'est qu'une demande). On retient donc le
    plus petit des deux — une alerte doit rester vraie meme au pire."""
    last = (state.get("sales") or {}).get(uid)
    if not last:
        return vf, None
    ls = _f(last[0])
    if ls <= 0:
        return vf, None
    return (min(vf, ls) if vf > 0 else ls), ls


def detect(state: Dict, listings: List[Dict], omi: float,
           veve: Dict[str, float], ts: float = None,
           journal: "Journal" = None) -> List[Dict]:
    """Un listing sous le floor = une affaire. Deux comparaisons :
      * marche StackR : price vs stackr_floor_price (tous deux en OMI) ;
      * marche VeVe   : price converti en $ vs floor VeVe (gems ~ $).
    Anti-bruit : listing deja vu, montant derisoire, cooldown par item.
    Chaque rejet est INSCRIT AU JOURNAL : un module qui se tait doit au moins
    pouvoir dire pourquoi."""
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
        if journal:
            journal.listings += 1
        usd = price * omi if omi else 0.0
        if usd and usd < MIN_USD:
            if journal:
                journal.rejet("broutille")
            continue                       # broutille : on ignore
        sf = _f(it.get("stackr_floor_price"))
        uid = str(it.get("element_id") or "")
        img = it.get("image_url") or ""
        if uid and sf > 0:
            # on memorise le floor StackR de l'item (pour le signal 3)
            state.setdefault("sfloors", {})[uid] = [
                sf, it.get("name") or uid[:8], it.get("rarity") or "", img]
        vf = veve.get(uid, 0.0)
        d_stackr = (100.0 * (sf - price) / sf) if sf > 0 else 0.0
        d_veve = (100.0 * (vf - usd) / vf) if (vf > 0 and usd > 0) else 0.0
        ref, last = _revente(vf, uid, state)
        net, marge = _marge(usd, ref)
        cand = {"name": it.get("name") or uid[:8], "usd": usd, "ref": ref,
                "net": net, "marge": marge, "ecart": d_veve,
                "d_stackr": d_stackr}
        arbitrage = (marge >= MARGIN_PCT and net >= MIN_PROFIT)
        # Un arbitrage suppose de REVENDRE au floor VeVe. Si l'item n'a AUCUNE
        # vente reelle connue, cette revente est une fiction. La sous-cotation
        # sur le MEME marche (floor StackR), elle, n'a pas besoin de preuve :
        # c'est une comparaison a offre egale.
        preuve_ok = (last is not None) or not REQUIRE_SALE
        sous_cote = d_stackr >= DROP_PCT
        if not sous_cote and not (arbitrage and preuve_ok):
            if arbitrage and not preuve_ok:
                state.setdefault("sans_vente", {})[uid] = ts
                if journal:
                    journal.rejet("illiquide", cand)
            elif journal:
                if vf <= 0:
                    journal.rejet("sans_floor")
                elif marge < MARGIN_PCT:
                    journal.rejet("marge", cand)
                elif net < MIN_PROFIT:
                    journal.rejet("profit", cand)
                else:
                    journal.rejet("sous_cote", cand)
            continue
        if ts - alerts.get(uid or nft, 0) < COOLDOWN_H * 3600:
            if journal:
                journal.rejet("cooldown")
            continue
        alerts[uid or nft] = ts
        out.append({"net": round(net, 2), "marge": round(marge, 1),
                    "revente": round(ref, 2), "last": last, "img": img,
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
                  ts: float = None, journal: "Journal" = None) -> List[Dict]:
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
    for uid, infos in list(sfloors.items()):
        sf, name, rarity = infos[0], infos[1], infos[2]
        img = infos[3] if len(infos) > 3 else ""
        if journal:
            journal.items.add(uid)
        vf = veve.get(uid, 0.0)
        sf_usd = sf * omi
        if sf_usd <= 0 or sf_usd < MIN_USD:
            if journal:
                journal.rejet("broutille", cle=uid)
            continue
        if vf <= 0:
            if journal:
                journal.rejet("sans_floor", cle=uid)
            continue
        ecart = 100.0 * (vf - sf_usd) / vf
        ref, last = _revente(vf, uid, state)
        net, marge = _marge(sf_usd, ref)
        cand = {"name": name, "usd": sf_usd, "ref": ref, "net": net,
                "marge": marge, "ecart": ecart, "d_stackr": 0.0}
        if ecart < SPREAD_PCT:
            if journal:
                journal.rejet("ecart", cand, cle=uid)
            continue
        if marge < MARGIN_PCT:
            if journal:
                journal.rejet("marge", cand, cle=uid)
            continue
        if net < MIN_PROFIT:
            if journal:
                journal.rejet("profit", cand, cle=uid)
            continue
        if REQUIRE_SALE and last is None:
            # aucune vente depuis ~7 jours : l'item NE SE VEND PAS. Revendre au
            # floor VeVe y est une fiction -> on se tait.
            state.setdefault("sans_vente", {})[uid] = ts
            if journal:
                journal.rejet("illiquide", cand, cle=uid)
            continue
        # MEME verrou que les listings (cle = uuid) : un item deja signale
        # comme listing ne doit PAS ressortir en "ecart de marches" — c'est la
        # MEME affaire vue sous deux angles.
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            if journal:
                journal.rejet("cooldown", cle=uid)
            continue
        alerts[uid] = ts
        out.append({"net": round(net, 2), "marge": round(marge, 1),
                    "revente": round(ref, 2), "last": last, "img": img,
                    "nft": "", "uuid": uid, "name": name, "rarity": rarity,
                    "edition": "", "price": sf, "usd": sf_usd,
                    "stackr_floor": sf, "veve_floor": vf,
                    "d_stackr": 0.0, "d_veve": round(ecart, 1),
                    "seller": "(floor du marche)", "spread": True})
    out.sort(key=lambda a: -a.get("net", 0))
    return out


COULEURS = {"stackr": 0x3498DB,      # bleu   : sous le floor StackR
            "veve": 0x2ECC71,        # vert   : revendable plus cher sur VeVe
            "spread": 0xF1C40F}      # jaune  : ecart entre marches

AVERTISSEMENT = ("⚠️ À titre indicatif — pas un conseil financier. Le floor "
                 "VeVe est un prix DEMANDÉ : la marge est un plafond, pas un "
                 "gain garanti. Les valeurs peuvent être inexactes.")


def _omi(x) -> str:
    return f"{x:,.0f} OMI".replace(",", " ")


def _usd(x) -> str:
    return f"~{x:,.2f} $".replace(",", " ")


def _embed(a: Dict) -> Dict:
    """Une carte par affaire, dans la forme demandee par Preda :

        📉 Sous le Floor
        BB-8 #1559
        −5.4 % sous le floor
        45 400 OMI (~11 $)
        Floor : 48 000 OMI (~11 $)
        Voir sur StackR →
    """
    nom = f"{a['name']}" + (f" #{a['edition']}" if a.get("edition") else "")
    lien = f"https://www.stackr.world/element/{a['uuid']}"
    lignes = []
    if a.get("spread"):
        kind, titre = "spread", "↔️ Écart entre marchés"
        lignes.append(f"**{nom}**")
        lignes.append(f"Acheter au floor StackR **{_omi(a['stackr_floor'])}** "
                      f"({_usd(a['usd'])})")
        lignes.append(f"Floor VeVe : **{a['veve_floor']:,.2f} $**"
                      .replace(",", " "))
    elif a.get("d_stackr", 0) >= DROP_PCT:
        kind, titre = "stackr", "📉 Sous le Floor"
        lignes.append(f"**{nom}**")
        lignes.append(f"**−{a['d_stackr']} % sous le floor**")
        lignes.append(f"{_omi(a['price'])} ({_usd(a['usd'])})")
        lignes.append(f"Floor : {_omi(a['stackr_floor'])} "
                      f"({_usd(a['stackr_floor'] * (a['usd'] / a['price']) if a['price'] else 0)})")
    else:
        kind, titre = "veve", "💰 Revendable plus cher sur VeVe"
        lignes.append(f"**{nom}**")
        lignes.append(f"Achat : **{_omi(a['price'])}** ({_usd(a['usd'])})")
        lignes.append(f"Floor VeVe : **{a['veve_floor']:,.2f} $**"
                      .replace(",", " "))
    if a.get("last"):
        lignes.append(f"Dernière vente réelle : **{a['last']:,.2f} $**"
                      .replace(",", " "))
    else:
        lignes.append("*Aucune vente récente vue*")
    if a.get("net", 0) > 0:
        lignes.append(f"→ **+{a['net']:,.2f} $ net (+{a['marge']} %)** "
                      f"après {FEE_PCT} % de frais".replace(",", " "))
    lignes.append(f"[Voir sur StackR →]({lien})")
    e = {"title": titre, "color": COULEURS[kind],
         "description": "\n".join(lignes)[:4000], "url": lien,
         "footer": {"text": AVERTISSEMENT}}
    if a.get("img"):
        e["thumbnail"] = {"url": a["img"]}
    return e


def notify(alerts: List[Dict]) -> int:
    """UN message par tour, 10 cartes maximum, et on RESPECTE le 429 de Discord
    (s'obstiner sur un rate limit, c'est ce qui fait bannir un webhook)."""
    if not alerts:
        return 0
    embeds = [_embed(a) for a in alerts[:10]]
    contenu = (f"🚨 **{len(alerts)} affaire(s)** — "
               + _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC"))
    if len(alerts) > 10:
        contenu += f" (10 affichées sur {len(alerts)})"
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye"
              + ("" if WEBHOOK else " : pas de DISCORD_WEBHOOK") + "]",
              flush=True)
        for a in alerts[:10]:
            preuve = (f"derniere vente {a['last']:,.2f} $" if a.get("last")
                      else "aucune vente vue")
            print(f"    {a['name']} #{a.get('edition') or '-'} — "
                  f"achat {a['usd']:,.2f} $ · {preuve} · "
                  f"+{a['net']:,.2f} $ net", flush=True)
        return len(alerts)
    for essai in range(3):
        try:
            r = requests.post(WEBHOOK,
                              json={"content": contenu, "embeds": embeds},
                              timeout=20)
            if r.status_code == 429:
                attente = 5.0
                try:
                    attente = float(r.json().get("retry_after", 5)) + 1
                except Exception:
                    pass
                print(f"  Discord : rate limit — pause de {attente:.0f} s.",
                      flush=True)
                time.sleep(min(attente, 60))
                continue
            r.raise_for_status()
            print(f"  Discord : {len(alerts)} alerte(s) poussee(s).",
                  flush=True)
            return len(alerts)
        except Exception as e:
            print(f"  Discord KO ({e})", flush=True)
            if essai == 2:
                break
            time.sleep(5)
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
    journal = Journal()
    comics = charger_comics()
    if comics:
        print(f"📚 {len(comics)} element(s) de comics a tirage ≤ "
              f"{COMIC_SUPPLY_MAX} suivis (alerte sous {COMIC_MAX_USD:g} $).",
              flush=True)
    s = requests.Session()
    veve: Dict[str, float] = {}
    dernier_refresh = 0.0
    total = 0
    for i in range(1, POLLS + 1):
        omi = fetch_omi_price(s)
        # floors VeVe + historique des ventes : rafraichis 1x/heure
        if time.time() - dernier_refresh > REFRESH_MIN * 60:
            neuf = fetch_veve_floors(s)
            if neuf:
                veve = neuf
                dernier_refresh = time.time()
                print(f"  floors VeVe rafraichis : {len(veve)} elements.",
                      flush=True)
            hist = fetch_history(s, SALES_PAGES, omi)
            n_h = merge_history(state, hist)
            print(f"  historique : {len(hist)} elements ont une vente reelle "
                  f"({n_h} nouveaux) — les autres sont juges illiquides.",
                  flush=True)
        nv = note_sales(state, fetch_sales(s), omi)   # ventes REELLES
        listings = fetch_listings(s)
        if not listings:
            print(f"  [{i}/{POLLS}] aucun listing recu — on reessaiera.",
                  flush=True)
        else:
            a = detect(state, listings, omi, veve, journal=journal)
            a += detect_spread(state, veve, omi, journal=journal)  # signal 3
            print(f"  [{i}/{POLLS}] {len(listings)} listings, {nv} vente(s), "
                  f"{len(a)} alerte(s), OMI={omi:.6f} $", flush=True)
            total += notify(a)
            c = detect_comics(state, comics, veve, listings, omi)
            if c:
                print(f"  [{i}/{POLLS}] 📚 {len(c)} comic(s) a petit tirage "
                      f"sous {COMIC_MAX_USD:g} $ !", flush=True)
                total += notify_comics(c)
            save_state(state)
        if i < POLLS:
            time.sleep(INTERVAL_S)
    print(f"Termine : {POLLS} tours, {total} alerte(s), "
          f"{time.time() - t0:.0f}s.", flush=True)
    # ZERO ALERTE N'EST PAS UNE REPONSE : le journal dit QUEL verrou a serre.
    print(journal.resume(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN floor_watch.py v15 (le lien comic = uuid de l'ELEMENT, pas de la serie)
# v14 (+ signal 4 : le comic a petit tirage brade)\n# v13 — le module DIT pourquoi il se tait (journal des
# recales) et sait tourner a blanc (FLOOR_SIMULER) : on regle sur des chiffres,
# pas sur des suppositions.
