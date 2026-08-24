# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/stackr_fiches.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""📊 LES FICHES STACKR — les deux planchers, les offres en cours, les brulees.

Preda, 24/08/2026, capture a l'appui :
  « sur stackr il y a aussi le floor veve et le floor stackr en Omi et en $,
    il faudra collecter l'infos »

═══════════════════════════════════════════════════════════════════════════
CE QUI A ETE MESURE LE 24/08, ET COMMENT
═══════════════════════════════════════════════════════════════════════════
Les noms des procedures ne se devinent pas : neuf essais plausibles ont rendu
neuf 404 (avec un temoin `zzzInexistante` qui rendait 404 lui aussi — donc
l'instrument distinguait bien). Ils ont ete LUS dans les 39 fragments
JavaScript de leur site, 4,9 Mo au total. Le routeur public en declare treize ;
quatre nous interessent :

  publicVeve.getCollectible        {id}                    -> la fiche
  publicVeve.getComicCover         {id}                    -> idem, comics
  publicVeve.getElementListings_v2 {elementId, limit, ...}  -> les offres
  publicVeve.getElementTopHolders  {id, limit}              -> les detenteurs

⭐ TOUTES PUBLIQUES, SANS COOKIE. Mesure : 20 items d'affilee, 20 succes,
  0,71 s par item pause comprise.

CE QUE LA FICHE PORTE, ET QUE NOUS N'AVIONS PAS :
  floor_market_price   le plancher VeVe   — en DOLLARS
  stackr_floor_price   le plancher StackR — en OMI
  in_circulation       exemplaires en circulation
  editions_burnt       exemplaires BRULES
  issued               exemplaires emis
  market_fee           la commission du marche
  floor_updated_at     (comics) QUAND le plancher VeVe a bouge

⚠️ LA CAPTURE DE PREDA MONTRE QUATRE CHIFFRES, LA SOURCE N'EN DONNE QUE DEUX.
  Son ecran affiche « 3 365 384 OMI ($700.00) » et « 2 000 000 OMI ($416.00) ».
  L'API ne rend QUE `floor_market_price` (700, en $) et `stackr_floor_price`
  (2 000 000, en OMI). Les deux autres sont CALCULES par leur site avec le
  cours OMI/USD — 700 / 0,000208 = 3 365 384, et le compte tombe juste.
  ⇒ ⛔ ON NE COLLECTE PAS LES QUATRE : on collecte les deux vrais, et le site
    convertit avec `omi_usd.csv` (lot 181). Collecter une valeur derivee, c'est
    figer un cours dans un fichier qui vieillira sans le dire.
  ⭐⭐⭐ *Deux chiffres a l'ecran ne sont pas deux mesures : ce sont parfois la
    meme mesure vue dans deux unites.*

⛔⛔ ET CE N'EST PAS LA CONVERSION QUE CE PROJET INTERDIT. Ce qui est interdit
  — dans `cote.mjs`, dans `warehouse.mjs` et ici — c'est de deduire le plancher
  d'un MARCHE du plancher de l'AUTRE : `sfloors` (OMI) contre `vfloors` (USD)
  ont un rapport non constant (mediane 4 423, p10 2 273, p90 8 520 sur 1 306
  items communs). Convertir un montant DANS SA PROPRE DEVISE avec un cours cote
  n'est pas une deduction, c'est une observation de marche. C'est l'arbitrage
  du lot 181, deja tranche.

═══════════════════════════════════════════════════════════════════════════
🔴🔴 POURQUOI UNE ROTATION, ET PAS UN BALAYAGE COMPLET
═══════════════════════════════════════════════════════════════════════════
19 665 items x 0,71 s = 3 h 53 par tour complet, et 2 appels par item si l'on
veut aussi les offres. Un run quotidien de quatre heures sur un service tiers
est cher, peu discret, et il casserait au premier incident sans rien laisser.
⇒ BUDGET PAR RUN + REPRISE. L'etat retient ou l'on s'est arrete ; chaque run
  prend la tranche suivante. Le catalogue entier se boucle en quelques jours,
  et un run interrompu ne perd que sa tranche.
⭐ Conforme a la regle maison des collecteurs longs : etat persistant, jamais
  de perte, backoff, auto-controle.

⚠️ CE COLLECTEUR NE REMPLACE PAS `floor_watch.py`. Celui-la surveille le FLUX
  des nouvelles mises en vente toutes les 2 minutes — c'est un capteur
  d'evenements. Celui-ci prend une PHOTO de l'etat de chaque fiche, lentement.
  Deux questions, deux cadences, deux fichiers. ⛔ Les fusionner ferait qu'un
  balayage lent retarderait les alertes de prix.

Env :
  FICHES_BUDGET      items par run (def. 2000)
  FICHES_PAUSE       pause entre appels, secondes (def. 0.25)
  FICHES_OFFRES      collecter aussi le nombre d'offres (def. true, +1 appel)
  FICHES_HOLDERS     collecter les 5 premiers detenteurs (def. true, +1 appel)
  FICHES_STATE       etat de reprise (def. data/fiches_stackr_state.json)
  FICHES_OUT         sortie (def. data/fiches_stackr.csv)
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://www.stackr.world"
TRPC = BASE + "/api/trpc/publicVeve."
SITEMAPS = (
    (BASE + "/sitemap-collectibles.xml", "collectible"),
    (BASE + "/sitemap-comic-covers.xml", "comic"),
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Referer": BASE + "/"}

BUDGET = int(os.environ.get("FICHES_BUDGET", "2000"))
PAUSE = float(os.environ.get("FICHES_PAUSE", "0.25"))
AVEC_OFFRES = os.environ.get("FICHES_OFFRES", "true").lower() != "false"
# 👥 ALLUME PAR DEFAUT DEPUIS LE 24/08 (« oui » de Preda). ⚠️ C'est un
#   TROISIEME appel par piece : le tour complet passe de ~4 h a ~6 h de temps
#   machine cumule, reparties sur la rotation. `FICHES_HOLDERS=false` l'eteint
#   sans rien casser — le fichier des detenteurs cesse simplement de bouger.
AVEC_HOLDERS = os.environ.get("FICHES_HOLDERS", "true").lower() != "false"
STATE_PATH = os.environ.get("FICHES_STATE", "data/fiches_stackr_state.json")
OUT_PATH = os.environ.get("FICHES_OUT", "data/fiches_stackr.csv")
HOLDERS_PATH = os.environ.get("FICHES_HOLDERS_OUT", "data/holders_stackr.csv")

# ⚠️ UN UUID, ET RIEN D'AUTRE. Ces identifiants viennent d'un XML distant et
#   servent a composer une URL : liste blanche de FORME, jamais liste noire.
RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

COLONNES = [
    "veve_uuid", "famille", "ts_releve",
    "floor_veve_usd", "floor_stackr_omi",
    "in_circulation", "editions_burnt", "issued",
    "offres_en_cours", "market_fee", "floor_maj",
]


def _f(v: Any) -> Optional[float]:
    """Un nombre, ou None. ⚠️ JAMAIS 0 par defaut.

    🔴 PIEGE MESURE LE 24/08 : `floor_market_price` est un NOMBRE sur un
    collectible (700) et une CHAINE sur un comic ('8.98000000000000000000').
    Le meme champ, deux types, dans la meme API. Un `float()` nu casse sur
    l'un des deux ; un `or 0` transformerait « inconnu » en « gratuit ».
    ⭐⭐⭐ *Un zero invente est indistinguable d'un zero mesure*, et sur un
    site de cotes c'est la faute qu'on ne rattrape pas.
    """
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def _i(v: Any) -> Optional[int]:
    x = _f(v)
    return None if x is None else int(x)


class Client:
    """tRPC public, avec backoff. ⛔ Ne leve pas : un item illisible est SAUTE
    et COMPTE. Un balayage de 2 000 items ne doit pas mourir sur le 37e."""

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.appels = 0
        self.erreurs = 0

    def get(self, proc: str, payload: Dict[str, Any],
            meta: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        inp: Dict[str, Any] = {"json": payload}
        if meta:
            inp["meta"] = meta
        url = TRPC + proc + "?input=" + urllib.parse.quote(json.dumps(inp))
        # ⭐ TROIS ESSAIS, ATTENTE QUI DOUBLE. Un 429 ou une coupure ne doit ni
        #   perdre l'item ni marteler le service d'en face.
        attente = 1.0
        for essai in range(3):
            self.appels += 1
            try:
                r = self.s.get(url, timeout=25)
            except requests.RequestException as e:
                if essai == 2:
                    print(f"    [fiches] {proc}: {e}", flush=True)
                    self.erreurs += 1
                    return None
                time.sleep(attente); attente *= 2
                continue
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                try:
                    return r.json()["result"]["data"]["json"]
                except (KeyError, ValueError):
                    self.erreurs += 1
                    return None
            # 5xx et 429 : on retente. 4xx (sauf 429) : c'est definitif.
            if r.status_code < 500 and r.status_code != 429:
                self.erreurs += 1
                return None
            if essai == 2:
                print(f"    [fiches] {proc}: HTTP {r.status_code}", flush=True)
                self.erreurs += 1
                return None
            time.sleep(attente); attente *= 2
        return None

    def fiche(self, uuid: str, famille: str) -> Optional[Dict[str, Any]]:
        proc = "getComicCover" if famille == "comic" else "getCollectible"
        d = self.get(proc, {"id": uuid})
        return d if isinstance(d, dict) else None

    def offres(self, uuid: str) -> Optional[int]:
        """Le NOMBRE d'offres en cours. ⭐ `limit: '1'` : on ne veut que le
        compteur, pas la liste — une page de 50 couterait le meme appel mais
        cinquante fois plus d'octets a lire et a jeter.

        ⚠️ `limit` EST UNE CHAINE. Mesure du 24/08 : un entier rend
        « expected string, received number » en HTTP 400. C'est le meme piege
        que `getAllLatestListings_v2` dans `floor_watch.py`.
        """
        d = self.get("getElementListings_v2",
                     {"elementId": uuid, "globalFilter": None, "limit": "1"},
                     meta={"values": {"globalFilter": ["undefined"]}})
        if not isinstance(d, dict):
            return None
        # ⭐ DEUX ENDROITS PORTENT LE COMPTE, ET ILS NE SE VALENT PAS.
        #   `totalCount` est a la racine ; `total` est sur chaque ligne. Sur un
        #   item SANS offre, `items` est vide — donc `total` n'existe pas, et
        #   seul `totalCount` peut dire « zero ». Lire la ligne rendrait None
        #   la ou la reponse dit clairement 0 : « aucune offre » deviendrait
        #   « on ne sait pas », et la colonne se viderait sur les items les
        #   plus interessants (ceux que plus personne ne vend).
        n = _i(d.get("totalCount"))
        if n is None:
            items = d.get("items") or []
            n = _i(items[0].get("total")) if items else None
        return n

    def holders(self, uuid: str, limit: int = 5) -> List[Dict[str, Any]]:
        d = self.get("getElementTopHolders", {"id": uuid, "limit": limit})
        return d if isinstance(d, list) else []


def charger_sitemaps(cli: Client) -> List[Tuple[str, str]]:
    """La liste des items, telle que StackR la publie.

    ⭐⭐ LA SOURCE DE VERITE DE « QUI EXISTE CHEZ EUX », C'EST LEUR SITEMAP,
    PAS NOTRE CATALOGUE. Mesure du 24/08 : 2 756 collectibles + 17 014
    comic-covers = 19 770 adresses, et les 6 627 items a plancher StackR de
    notre propre etat y sont TOUS (100 %). En revanche seulement 93 % de nos
    items a plancher VeVe y figurent : partir de notre catalogue produirait
    ~470 appels certains d'echouer, chaque jour.
    """
    out: List[Tuple[str, str]] = []
    for url, famille in SITEMAPS:
        try:
            r = cli.s.get(url, timeout=90)
            r.raise_for_status()
        except requests.RequestException as e:
            # ⛔ ON NE CONTINUE PAS SUR UN SITEMAP MANQUANT. Un balayage sur la
            #   moitie du catalogue ressemblerait a un balayage complet dans le
            #   fichier de sortie, et personne ne verrait la moitie absente.
            print(f"⛔ sitemap illisible ({url}) : {e}", file=sys.stderr, flush=True)
            return []
        motif = r"/(?:collectible|comic-cover)/([0-9a-f-]{36})"
        vus = 0
        for m in re.finditer(motif, r.text):
            u = m.group(1)
            if RE_UUID.match(u):
                out.append((u, famille))
                vus += 1
        print(f"  {famille}: {vus} adresse(s)", flush=True)
    return out


def charger_etat() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ecrire_etat(st: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def charger_sortie() -> Dict[str, Dict[str, Any]]:
    """Les lignes deja ecrites, par uuid.

    ⭐⭐⭐ LE FICHIER EST CUMULATIF, ET C'EST TOUT L'INTERET DE LA ROTATION.
    Sans relecture, chaque run ECRASERAIT le fichier avec sa seule tranche : on
    servirait 2 000 items au lieu de 19 770, et le fichier aurait exactement
    l'air d'un fichier complet. ⛔ Le piege est silencieux : rien ne casse.
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with open(OUT_PATH, encoding="utf-8", newline="") as f:
            for ligne in csv.DictReader(f):
                u = (ligne.get("veve_uuid") or "").strip()
                if RE_UUID.match(u):
                    out[u] = ligne
    except (OSError, ValueError):
        pass
    return out


def ecrire_sortie(lignes: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES, extrasaction="ignore")
        w.writeheader()
        for u in sorted(lignes):
            w.writerow(lignes[u])


COLONNES_HOLDERS = ["veve_uuid", "wallet", "username", "issues_owned", "ts_releve"]


def charger_holders() -> Dict[Tuple[str, str], List[Any]]:
    """Les detenteurs deja connus, cles par (piece, portefeuille).

    ⭐⭐ MEME RAISON QUE `charger_sortie()`, ET LE MEME PIEGE : sans relecture,
    chaque run ecraserait le fichier avec sa tranche. Apres dix jours de
    rotation on servirait 2 000 pieces sur 19 774, et le fichier aurait
    exactement l'air d'un fichier complet.

    ⚠️ LA CLE EST LE COUPLE, PAS LE PORTEFEUILLE SEUL. Un meme portefeuille
    detient des pieces differentes — le prendre pour cle ne garderait que la
    derniere piece vue de chaque collectionneur, et le fichier retrecirait
    silencieusement a mesure qu'il se remplit.
    """
    out: Dict[Tuple[str, str], List[Any]] = {}
    try:
        with open(HOLDERS_PATH, encoding="utf-8", newline="") as f:
            for l in csv.DictReader(f):
                u = (l.get("veve_uuid") or "").strip()
                w = (l.get("wallet") or "").strip().lower()
                # ⛔ MEME LISTE BLANCHE QU'AILLEURS : ce fichier vient de la
                #   Release, c'est une donnee d'entree comme une autre.
                if RE_UUID.match(u) and w.startswith("0x"):
                    out[(u, w)] = [u, w, l.get("username") or "",
                                   l.get("issues_owned") or "", l.get("ts_releve") or ""]
    except (OSError, ValueError):
        pass
    return out


def ecrire_holders(neuves: List[List[Any]]) -> None:
    """Fusionne la tranche dans le fichier cumulatif, puis reecrit.

    ⚠️ UNE PIECE REVUE REMPLACE SES ANCIENNES LIGNES, ELLE NE S'Y AJOUTE PAS.
    Sans ce menage, un collectionneur qui a TOUT vendu resterait inscrit comme
    detenteur pour toujours — le fichier accumulerait des proprietaires qui ne
    possedent plus rien, et le classement des plus gros porteurs serait faux.
    ⛔ On ne purge QUE les pieces revues dans cette tranche : les autres n'ont
      pas ete mesurees, et « pas mesure » n'est pas « plus de detenteurs ».
    """
    connues = charger_holders()
    revues = {l[0] for l in neuves}
    if revues:
        for cle in [k for k in connues if k[0] in revues]:
            connues.pop(cle, None)
    for l in neuves:
        connues[(l[0], str(l[1]).lower())] = l
    if not connues:
        return
    os.makedirs(os.path.dirname(HOLDERS_PATH) or ".", exist_ok=True)
    with open(HOLDERS_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLONNES_HOLDERS)
        for cle in sorted(connues):
            w.writerow(connues[cle])


def ligne_de(uuid: str, famille: str, d: Dict[str, Any],
             offres: Optional[int], ts: float) -> Dict[str, Any]:
    """Une fiche brute -> une ligne. ⛔ AUCUN CHAMP INVENTE : ce qui manque
    reste VIDE, jamais zero. Une colonne vide se voit ; un zero se croit."""
    vide = ""
    def s(x):
        return vide if x is None else x
    return {
        "veve_uuid": uuid,
        "famille": famille,
        "ts_releve": "%.0f" % ts,
        # ⚠️ LES DEUX UNITES SONT DANS LE NOM DE LA COLONNE, pas dans une
        #   colonne `unite` a part : ici chaque ligne porte les DEUX marches,
        #   donc une seule colonne d'unite serait ambigue. (Dans `releves.csv`,
        #   une ligne = un marche, et la colonne `unite` y a du sens.)
        "floor_veve_usd": s(_f(d.get("floor_market_price"))),
        "floor_stackr_omi": s(_f(d.get("stackr_floor_price"))),
        "in_circulation": s(_i(d.get("in_circulation"))),
        "editions_burnt": s(_i(d.get("editions_burnt"))),
        "issued": s(_i(d.get("issued"))),
        "offres_en_cours": s(offres),
        "market_fee": s(_f(d.get("market_fee"))),
        # ⚠️ Present sur les comics, absent des collectibles (mesure du 24/08).
        #   Une colonne vide sur une famille entiere est une INFORMATION, pas
        #   un defaut — a condition de ne pas la remplir avec autre chose.
        "floor_maj": s(d.get("floor_updated_at")),
    }


def main() -> int:
    t0 = time.time()
    cli = Client()

    print("📊 fiches StackR — les deux planchers, les offres, les brulees", flush=True)
    items = charger_sitemaps(cli)
    if not items:
        print("⛔ aucun item : rien n'a ete ecrit, l'etat n'a pas bouge.",
              file=sys.stderr, flush=True)
        return 1
    print(f"  total publie par StackR : {len(items)} item(s)", flush=True)

    etat = charger_etat()
    depart = int(etat.get("curseur") or 0)
    # ⚠️ LE CURSEUR SE BORNE A LA TAILLE COURANTE. Leur catalogue grandit et
    #   retrecit ; un curseur herite d'un tour plus long pointerait dans le
    #   vide, et le run ne ferait RIEN en rendant un succes.
    if depart >= len(items):
        depart = 0
    tranche = items[depart:depart + BUDGET]
    print(f"  tranche : {depart} -> {depart + len(tranche)} "
          f"(budget {BUDGET}, {'avec' if AVEC_OFFRES else 'sans'} les offres)", flush=True)

    lignes = charger_sortie()
    avant = len(lignes)
    ts = time.time()
    ok = 0
    rates = 0
    holders_lignes: List[List[Any]] = []

    for uuid, famille in tranche:
        d = cli.fiche(uuid, famille)
        if not d:
            rates += 1
            time.sleep(PAUSE)
            continue
        n = cli.offres(uuid) if AVEC_OFFRES else None
        lignes[uuid] = ligne_de(uuid, famille, d, n, ts)
        ok += 1
        if AVEC_HOLDERS:
            for h in cli.holders(uuid):
                holders_lignes.append([uuid, h.get("id") or "",
                                       h.get("username") or "",
                                       _i(h.get("issues_owned")) or 0,
                                       "%.0f" % ts])
        time.sleep(PAUSE)

    ecrire_sortie(lignes)
    if AVEC_HOLDERS:
        # 🔴🔴🔴 CE BLOC A ETE ECRIT DEUX FOIS, ET LA PREMIERE VERSION ETAIT
        #    FAUSSE — ELLE ECRASAIT LE FICHIER AVEC SA SEULE TRANCHE.
        # Elle est restee invisible tant que les detenteurs etaient ETEINTS par
        # defaut : le chemin ne s'executait jamais. Preda a repondu « oui » le
        # 24/08, et le defaut est apparu en meme temps que la fonctionnalite.
        # ⭐⭐⭐ *Un chemin de code jamais emprunte n'est pas un chemin sûr :
        #   c'est un chemin non mesure.* Le CSV principal avait sa relecture
        #   cumulative depuis le debut ET une faute injectee qui la tenait ;
        #   celui-ci n'avait ni l'une ni l'autre, parce qu'il dormait.
        # ⇒ Meme regle que `charger_sortie()` : on RELIT, on FUSIONNE, on
        #   reecrit. Sans ca, apres dix jours de rotation, le fichier
        #   contiendrait 2 000 pieces sur 19 774 en ayant l'air complet.
        ecrire_holders(holders_lignes)

    # ⭐⭐ LE CURSEUR N'AVANCE QU'APRES L'ECRITURE. S'il avancait avant, un run
    #   qui meurt en ecrivant sauterait sa tranche POUR TOUJOURS — et le trou
    #   ne se verrait que le jour ou quelqu'un compterait les lignes.
    #   *Ou l'etat est-il ecrit quand ca rate ?* — la question de la maison.
    etat["curseur"] = (depart + len(tranche)) % max(len(items), 1)
    etat["dernier_run"] = ts
    etat["total_publie"] = len(items)
    ecrire_etat(etat)

    # ⭐ IL COMPTE CE QUI A ETE ECRIT, et il dit la COUVERTURE — seule facon de
    #   savoir si le fichier est complet ou s'il n'a vu qu'une tranche.
    couv = 100.0 * len(lignes) / len(items) if items else 0.0
    avec_offres = sum(1 for l in lignes.values() if str(l.get("offres_en_cours") or "") != "")
    avec_burnt = sum(1 for l in lignes.values()
                     if str(l.get("editions_burnt") or "") not in ("", "0"))
    print(f"fiches_stackr.csv : {len(lignes)} ligne(s) (+{len(lignes) - avant} neuve(s)) "
          f"· couverture {couv:.1f} % du catalogue StackR "
          f"· {avec_offres} avec un nombre d'offres "
          f"· {avec_burnt} avec des editions brulees", flush=True)
    if AVEC_HOLDERS:
        # ⭐ IL COMPTE LE FICHIER, PAS LA TRANCHE. « 47 detenteurs ecrits »
        #   ne dit pas si les 19 000 autres pieces sont encore la ; le total
        #   cumule, si.
        tot = len(charger_holders())
        print(f"holders_stackr.csv : {tot} couple(s) piece/portefeuille "
              f"(+{len(holders_lignes)} vu(s) sur cette tranche)", flush=True)
    print(f"  tranche : {ok} succes, {rates} echec(s) · {cli.appels} appel(s), "
          f"{cli.erreurs} en erreur · {time.time() - t0:.0f} s "
          f"· prochain depart : {etat['curseur']}", flush=True)
    if couv < 99.0:
        # ⚠️ CE N'EST PAS UNE ALERTE, C'EST UNE EXPLICATION. Un fichier
        #   incomplet est l'etat NORMAL pendant les premiers jours de rotation.
        print(f"  ℹ️ couverture partielle : c'est attendu tant que la rotation "
              f"n'a pas boucle ({len(items)} items / {BUDGET} par run "
              f"= {max(1, -(-len(items) // max(BUDGET, 1)))} run(s)).", flush=True)
    # ⛔ UN RUN QUI N'A RIEN COLLECTE SORT EN ECHEC. Sans ca, un blocage cote
    #   StackR (403 sur tout) rendrait un run VERT avec un fichier fige, et
    #   personne ne le verrait avant des semaines.
    if ok == 0:
        print("⛔ aucune fiche collectee sur cette tranche.", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
