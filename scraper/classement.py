"""
🏆A-CLASSEMENT — COMICS (1 ligne par serie) + COLLECTIBLES (1 ligne par piece).

LA PAGE
    1. 🆕 A NOTER — COMICS       les 7 derniers jours + les drops a venir, non notes,
                                 hors mercredi (VVBD : Preda ne les note pas).
    2. 🎨 A NOTER — COLLECTIBLES les 7 derniers jours + a venir, non notes, hors AP.
    3. 🏆 CLASSEMENT             tout le reste, trie par score. Les ⚠️ non notes en
                                 fond rouge (qui s'efface des qu'on remplit), les 📅
                                 du mercredi et les 🎨 AP en bas, silencieux.

DEUX GRILLES, UNE SEULE PAGE
    Un comic s'ancre sur la VALEUR IRL du 9.8 physique. Un collectible n'a pas
    d'equivalent : il s'ancre sur le FLOOR du marche — mais seulement si ce floor
    est adosse a assez d'offres.

    COMIC        note = bande(valeur IRL, x3) + crans(supply) + bonus_perso
    COLLECTIBLE  note = bande(floor, x3) + crans(supply) + FA/FE + licence + bonus
                 floor retenu seulement si >= 7 offres (1 085 collectibles sur 2 656).
                 Sans floor fiable : on part d'un niveau NEUTRE et seuls le supply,
                 la FA/FE et la licence decident. On n'invente pas une valeur.

    LE FLOOR DES COMICS EST ECARTE. Mesure faite le 13/07 sur 80 014 relevés : le
    nombre d'offres moyen est de 0,04 (maximum 4). Un prix affiche adosse a zero
    offre n'est pas un prix. Chez les collectibles : 9,5 offres en moyenne, jusqu'a
    147 — la, ça veut dire quelque chose.

CE QUI EST A TOI (jamais ecrase)
    valeur_irl_98 · fa_key · bonus_perso · note · commentaire
    Relus par NOM DE COLONNE (jamais par position) et revalides : une saisie qui ne
    ressemble pas a ce qu'elle devrait etre est rejetee et reprise depuis 🔗A-RACCORD.

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, JOURS_RECENTS (7)
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COMICS_TAB = "🟢C-COMICS"
COLLECT_TAB = "🔵C-COLLECTIBLE"
DYN_STATE_TAB = "_DynState"          # dernier floor connu par uuid (ecrit par floors.py)
RACCORD_TAB = "🔗A-RACCORD"
CLASSEMENT_TAB = "🏆A-CLASSEMENT"

# ---------------------------------------------------------------------------
# LES GRILLES — tout se regle ici
# ---------------------------------------------------------------------------
ECHELLE = ["C", "CC", "CCC", "B", "BB", "BBB", "A", "AA", "AAA"]
RATIO = 3.0                     # un cran = x3 sur l'ancre de valeur

BASE_COMIC = 35.0               # frontiere C/CC d'un comic, en $ (valeur du 9.8)
PALIERS_COMIC = [               # (supply STRICTEMENT INFERIEUR A, crans)
    (1000, +2), (2001, +1), (5001, 0), (10001, -1), (15001, -2), (float("inf"), -3),
]

BASE_COLLECT = 5.0              # frontiere C/CC d'un collectible, en gems (floor)
PALIERS_COLLECT = [             # l'echelle des collectibles n'a rien a voir : les
    (100, +4),                  # supplys vont de 1 (les 1/1) a ~30 000
    (251, +3), (1001, +2), (2501, +1), (5001, 0), (10001, -1), (30001, -2),
    (float("inf"), -3),
]
OFFRES_MINI = 7                 # en dessous, le floor n'est pas une donnee
FLOOR_MAX = 1_000_000           # au-dela c'est une offre delirante (cf. le Golden MYO
                                # a 111 Md$) : un floor est un prix DEMANDE.
NIVEAU_SANS_FLOOR = 3           # le niveau du collectible MOYEN qui a un floor fiable
                                # (floor moyen 52 gems -> niveau 3). Sans marche, on
                                # part de la mediane, pas de zero.

CRANS_EDITION = {"FA": 3, "FE": 2, "CE": 2}      # AP = jamais note (voir ci-dessous)
LICENCES_BONUS = {"Marvel": 1, "Star Wars": 1, "DC Direct": 1, "DC": 1,
                  "Disney": 1, "Pixar": 1}
EDITION_NON_NOTEE = "AP"        # Artist Proof : des 1/1 que VeVe range en collectibles
                                # alors que ce sont des ARTWORKS. Preda ne les note pas.

JOUR_VVBD = 2                   # mercredi : le VeVe Comic Book Day, jamais note

MANUELLES = ["valeur_irl_98", "fa_key", "bonus_perso", "note", "commentaire"]

HEADER = [
    "etat", "type", "nom", "date_drop", "era", "supply", "prix_gems",
    "edition_type", "cover_exclusive", "floor", "offres",
    "valeur_irl_98", "fa_key", "bonus_perso", "note",
    "note_suggeree", "ecart", "valeur_par_edition", "multiple_entree", "score",
    "commentaire", "rarity", "nb_raretes", "veve_brand", "veve_licensor",
    "start_year", "veve_url", "uuid",
]
I = {c: i for i, c in enumerate(HEADER)}
NB_COL = len(HEADER)                             # 28 -> derniere colonne = AB
CALC = ["note_suggeree", "ecart", "valeur_par_edition", "multiple_entree", "score"]

A_VENIR, RECENT, ALERTE, VVBD, ARTWORK = "🔴", "🆕", "⚠️", "📅", "🎨"

VIOLET = {"red": 0.42, "green": 0.35, "blue": 0.66}
BLANC = {"red": 1.0, "green": 1.0, "blue": 1.0}
FOND_COMICS = {"red": 1.0, "green": 0.94, "blue": 0.85}    # orange clair
FOND_COLLECT = {"red": 0.86, "green": 0.93, "blue": 1.0}   # bleu clair
FOND_ENTETE = {"red": 0.91, "green": 0.88, "blue": 0.96}
FOND_METHODE = {"red": 0.96, "green": 0.96, "blue": 0.96}
FOND_ALERTE = {"red": 0.99, "green": 0.89, "blue": 0.89}
GRIS = {"red": 0.40, "green": 0.40, "blue": 0.40}

URL_COMIC = "https://www.veve.me/collectibles/en/comics/{uuid}"
URL_COLLECT = "https://www.veve.me/collectibles/en/collectibles/{uuid}"
_EPOCH = _dt.datetime(1899, 12, 30)


def _col(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


DERNIERE = _col(NB_COL - 1)


# ---------------------------------------------------------------------------
# Les grilles, en Python
# ---------------------------------------------------------------------------

def _crans(supply: Optional[float], paliers) -> int:
    if not supply or supply <= 0:
        return 0
    for borne, crans in paliers:
        if supply < borne:
            return crans
    return paliers[-1][1]


def _niveau(valeur: float, base: float) -> int:
    """NON PLAFONNE : le niveau peut depasser AAA, c'est le supply qui redescend.
    Sans ca, Amazing Fantasy #15 (5 M$, supply 10 000) ne pouvait plus etre AAA."""
    if valeur < base:
        return 0
    return int(math.floor(math.log(valeur / base) / math.log(RATIO))) + 1


def floor_fiable(floor: Optional[float], offres: Optional[float]) -> bool:
    return bool(floor and offres and offres >= OFFRES_MINI and 0 < floor < FLOOR_MAX)


def note_comic(valeur, supply, bonus=0) -> str:
    if not valeur or not supply:
        return ""
    k = _niveau(valeur, BASE_COMIC) + _crans(supply, PALIERS_COMIC) + int(bonus or 0)
    return ECHELLE[max(0, min(8, k))]


def note_collectible(floor, offres, supply, edition, licencier, bonus=0) -> str:
    if str(edition).strip().upper() == EDITION_NON_NOTEE or not supply:
        return ""
    niv = (_niveau(floor, BASE_COLLECT) if floor_fiable(floor, offres)
           else NIVEAU_SANS_FLOOR)
    k = (niv + _crans(supply, PALIERS_COLLECT)
         + CRANS_EDITION.get(str(edition).strip().upper(), 0)
         + LICENCES_BONUS.get(str(licencier).strip(), 0) + int(bonus or 0))
    return ECHELLE[max(0, min(8, k))]


def score_comic(valeur, supply) -> Any:
    if not valeur or not supply:
        return ""
    return round(valeur * (RATIO ** _crans(supply, PALIERS_COMIC)), 1)


def score_collectible(floor, offres, supply, edition, licencier) -> Any:
    if not supply or not floor_fiable(floor, offres):
        return ""
    crans = (_crans(supply, PALIERS_COLLECT)
             + CRANS_EDITION.get(str(edition).strip().upper(), 0)
             + LICENCES_BONUS.get(str(licencier).strip(), 0))
    return round(floor * (RATIO ** crans), 1)


# --- les memes grilles, VIVANTES dans le Sheet -----------------------------
# La note se recalcule pendant que Preda tape. Les formules sont GENEREES depuis les
# constantes ci-dessus : le Sheet ne peut pas diverger du Python.
# ⚠️ Locale FR : separateur « ; » (une virgule donne #ERROR!).

def _f_crans(col: str, paliers) -> str:
    bornes = [(b, c) for b, c in paliers if b != float("inf")]
    f = str(paliers[-1][1])
    for borne, crans in reversed(bornes):
        f = f"IF({col}<{borne:.0f};{crans};{f})"
    return f


def _f_niveau(col: str, base: float) -> str:
    return f"IF({col}<{base:.0f};0;FLOOR(LN({col}/{base:.0f})/LN(3))+1)"


def _f_edition(col: str) -> str:
    f = "0"
    for ed, crans in CRANS_EDITION.items():
        f = f'IF({col}="{ed}";{crans};{f})'
    return f


def _f_licence(col: str) -> str:
    noms = ";".join(f'{col}="{n}"' for n in LICENCES_BONUS)
    return f"IF(OR({noms});1;0)"


def formules(r: int) -> List[str]:
    """Les 5 colonnes calculees de la ligne r. Elles BRANCHENT sur le type."""
    typ, sup, gem = f"$B{r}", f"$F{r}", f"$G{r}"
    ed, flo, off = f"$H{r}", f"$J{r}", f"$K{r}"
    val, bon, note = f"$L{r}", f"$N{r}", f"$O{r}"
    sug, par_ed, lic = f"$P{r}", f"$R{r}", f"$Y{r}"
    ech = "{" + ";".join(f'"{x}"' for x in ECHELLE) + "}"
    b = f'IF({bon}="";0;{bon})'

    k_comic = (f"1+{_f_niveau(val, BASE_COMIC)}+{_f_crans(sup, PALIERS_COMIC)}+{b}")
    ancre = (f'IF(AND({off}>={OFFRES_MINI};{flo}>0;{flo}<{FLOOR_MAX});'
             f'{_f_niveau(flo, BASE_COLLECT)};{NIVEAU_SANS_FLOOR})')
    k_coll = (f"1+{ancre}+{_f_crans(sup, PALIERS_COLLECT)}"
              f"+{_f_edition(ed)}+{_f_licence(lic)}+{b}")
    borne = "MIN({n};MAX(1;{k}))".format(n=len(ECHELLE), k="{k}")

    note_sug = (
        f'=IF({typ}="comic";'
        f'  IF(OR({val}="";{sup}="");"";INDEX({ech};{borne.format(k=k_comic)}));'
        f'  IF(OR({sup}="";{ed}="{EDITION_NON_NOTEE}");"";'
        f'     INDEX({ech};{borne.format(k=k_coll)})))'
    )
    ecart = f'=IF(OR({note}="";{sug}="");"";MATCH({note};{ech};0)-MATCH({sug};{ech};0))'
    par_edition = f'=IF(OR({typ}<>"comic";{val}="";{sup}="");"";{val}/{sup})'
    # le multiple = ce que ça vaut / ce que tu l'as paye. Pour un comic : la valeur
    # adossee a chaque edition. Pour un collectible : le floor lui-meme.
    multiple = (f'=IF({gem}="";"";IF({typ}="comic";IF({par_ed}="";"";{par_ed}/{gem});'
                f'IF(OR({flo}="";{off}<{OFFRES_MINI});"";{flo}/{gem})))')
    score = (
        f'=IF({typ}="comic";'
        f'  IF(OR({val}="";{sup}="");"";{val}*POWER(3;{_f_crans(sup, PALIERS_COMIC)}));'
        f'  IF(OR({sup}="";{flo}="";{off}<{OFFRES_MINI};{flo}>={FLOOR_MAX});"";'
        f'     {flo}*POWER(3;{_f_crans(sup, PALIERS_COLLECT)}'
        f'+{_f_edition(ed)}+{_f_licence(lic)})))'
    )
    return [note_sug, ecart, par_edition, multiple, score]


def _methode_comics() -> str:
    b = [BASE_COMIC * (RATIO ** i) for i in range(8)]
    bandes = " · ".join(f"{n} {v:,.0f}$" for n, v in zip(ECHELLE[1:], b))
    return (
        "MÉTHODE COMICS — la note part de la VALEUR IRL du comic physique en 9.8 "
        "(un cran par ×3), puis le SUPPLY la décale : moins de 1 000 → +2 crans · "
        "1 000-2 000 → +1 · 2 000-5 000 → 0 · 5 000-10 000 → −1 · 10 000-15 000 → −2 · "
        f"plus de 15 000 → −3.   Bandes à supply neutre : C sous {BASE_COMIC:,.0f}$ · "
        f"{bandes}.   À REMPLIR : valeur_irl_98, fa_key, bonus_perso (± crans quand le "
        "personnage vaut plus que son comic : Venom +2, Miles Morales +2), note. "
        "Le comic quitte ce module dès que sa valeur ou sa note est remplie. "
        "Les comics du mercredi (VeVe Comic Book Day) n'y entrent jamais. "
        "Le FLOOR n'est pas utilisé pour les comics : 0,04 offre en moyenne — un prix "
        "adossé à zéro offre n'est pas un prix."
    ).replace(",", " ")


def _methode_collectibles() -> str:
    b = [BASE_COLLECT * (RATIO ** i) for i in range(8)]
    bandes = " · ".join(f"{n} {v:,.0f}" for n, v in zip(ECHELLE[1:], b))
    return (
        "MÉTHODE COLLECTIBLES — pas de valeur IRL : l'ancre est le FLOOR du marché, "
        f"retenu seulement s'il est adossé à au moins {OFFRES_MINI} offres. "
        f"Bandes de floor (en gems) : C sous {BASE_COLLECT:,.0f} · {bandes}.   "
        "Puis les décalages : SUPPLY (moins de 100 → +4 · 100-250 → +3 · 250-1 000 → "
        "+2 · 1 000-2 500 → +1 · 2 500-5 000 → 0 · 5-10k → −1 · 10-30k → −2 · plus de "
        "30k → −3) · ÉDITION (FA +3 · FE +2 · CE +2) · LICENCE (Marvel, Star Wars, DC, "
        "Disney/Pixar : +1) · bonus_perso.   Sans floor fiable, on part d'un niveau "
        "neutre et seuls le supply, la FA/FE et la licence décident — on n'invente pas "
        "une valeur.   Les AP (Artist Proofs) sont des artworks 1/1 : jamais notés."
    ).replace(",", " ")


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    if x in (None, ""):
        return None
    try:
        return float(str(x).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def prix_en_gems(x: Any, is_comic: bool) -> Optional[float]:
    """VeVe melange DEUX echelles dans `storePrice` cote COMICS : vieux comics en GEMS
    (10, 15, 20), comics recents en FIAT et en CENTIMES (699, 798, 1499). Preuve :
    Captain America Comics #7 = 699 au catalogue, et la carte Discord de Preda dit
    « 7 gems ». Au-dela de 100 = centimes. IDEMPOTENT.
    ⚠️ Comics seulement : un collectible a 1 500 gems, ça existe."""
    v = _num(x)
    if v is None:
        return None
    return round(v / 100, 2) if (is_comic and v >= 100) else v


def _date(x: Any) -> Optional[_dt.datetime]:
    """⚠️ Les catalogues sont lus en UNFORMATTED (obligatoire : locale FR, "6,99"
    serait numerise en 699). Dans ce mode une DATE revient en numero de serie Google
    (46 212,625), pas en texte."""
    if x in (None, ""):
        return None
    if isinstance(x, (int, float)):
        try:
            return _EPOCH + _dt.timedelta(days=float(x))
        except (ValueError, OverflowError):
            return None
    s = str(x).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _era(annee: Optional[int]) -> str:
    if not annee:
        return ""
    for limite, nom in ((1956, "Golden"), (1970, "Silver"), (1985, "Bronze"),
                        (1992, "Copper")):
        if annee < limite:
            return nom
    return "Modern"


def _client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON manquant.")
    return gspread.authorize(
        Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES))


def load_floors(sh) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """{veve_uuid: (floor, offres)} depuis _DynState (dernier etat connu)."""
    try:
        rows = sh.worksheet(DYN_STATE_TAB).get_all_records(
            value_render_option="UNFORMATTED_VALUE")
    except gspread.WorksheetNotFound:
        print("_DynState absent : aucun floor.", flush=True)
        return {}
    out = {}
    for r in rows:
        uid = str(r.get("veve_uuid", "") or "").strip()
        if uid:
            out[uid] = (_num(r.get("market_lowestOffer")),
                        _num(r.get("market_totalListings")))
    fiables = sum(1 for f, o in out.values() if floor_fiable(f, o))
    print(f"Floors : {len(out)} items, {fiables} adosses a >= {OFFRES_MINI} offres.",
          flush=True)
    return out


def load_comics(sh) -> Dict[str, Dict[str, Any]]:
    rows = sh.worksheet(COMICS_TAB).get_all_records(
        value_render_option="UNFORMATTED_VALUE")
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        uid = str(r.get("series_uuid", "") or "").strip()
        if not uid:
            continue
        s = out.get(uid)
        if s is None:
            annee = _num(r.get("start_year"))
            s = out[uid] = {
                "uuid": uid, "type": "comic",
                "nom": r.get("veve_series_name", ""),
                "date_drop": _date(r.get("releaseDate")),
                "era": _era(int(annee) if annee else None),
                "supply": _num(r.get("supply")),
                "prix_gems": prix_en_gems(r.get("store_price_gems"), True),
                "edition_type": "", "cover_exclusive": r.get("veve_exclusive", ""),
                "floor": None, "offres": None, "rarity": "",
                "veve_brand": r.get("veve_brand", ""),
                "veve_licensor": r.get("veve_licensor", ""),
                "start_year": int(annee) if annee else "",
                "veve_url": URL_COMIC.format(uuid=uid), "nb_raretes": 0,
            }
        s["nb_raretes"] += 1
    mercredi = sum(1 for s in out.values()
                   if s["date_drop"] and s["date_drop"].weekday() == JOUR_VVBD)
    print(f"Comics : {len(out)} series ({mercredi} du mercredi, jamais notees).",
          flush=True)
    return out


def load_collectibles(sh, floors) -> Dict[str, Dict[str, Any]]:
    rows = sh.worksheet(COLLECT_TAB).get_all_records(
        value_render_option="UNFORMATTED_VALUE")
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        uid = str(r.get("veve_uuid", "") or "").strip()
        if not uid:
            continue
        f, o = floors.get(uid, (None, None))
        out[uid] = {
            "uuid": uid, "type": "collectible", "nom": r.get("name", ""),
            "date_drop": _date(r.get("releaseDate")),
            "era": "", "supply": _num(r.get("supply")),
            "prix_gems": prix_en_gems(r.get("store_price_gems"), False),
            "edition_type": str(r.get("edition_type", "") or "").strip().upper(),
            "cover_exclusive": "", "floor": f, "offres": o,
            "rarity": r.get("rarity", ""),
            "veve_brand": r.get("veve_brand", ""),
            "veve_licensor": r.get("veve_licensor", ""),
            "start_year": "", "veve_url": URL_COLLECT.format(uuid=uid),
            "nb_raretes": 1,
        }
    ap = sum(1 for s in out.values() if s["edition_type"] == EDITION_NON_NOTEE)
    fa = sum(1 for s in out.values() if s["edition_type"] == "FA")
    print(f"Collectibles : {len(out)} pieces ({fa} FA, {ap} AP jamais notees).",
          flush=True)
    return out


def load_manuel(sh, connus: set) -> Dict[str, Dict[str, Any]]:
    manuel: Dict[str, Dict[str, Any]] = {}
    try:
        for r in sh.worksheet(RACCORD_TAB).get_all_records(
                value_render_option="UNFORMATTED_VALUE"):
            valide = str(r.get("valide", "") or "").strip()
            uid = (valide if len(valide) > 10
                   else str(r.get("series_uuid", "") or "").strip())
            if not uid or uid not in connus:
                continue
            if str(r.get("statut", "")).strip() != "CERTAIN" and not valide:
                continue
            manuel.setdefault(uid, {
                "valeur_irl_98": r.get("valeur_irl", ""),
                "fa_key": r.get("fa_preda", ""), "bonus_perso": "",
                "note": r.get("note", ""), "commentaire": "",
            })
        print(f"Amorce depuis le classement : {len(manuel)} series.", flush=True)
    except gspread.WorksheetNotFound:
        print("🔗A-RACCORD absent : aucune amorce.", flush=True)

    try:
        ws = sh.worksheet(CLASSEMENT_TAB)
        repris, rejetes = 0, 0
        # ⚠️ LECTURE PAR NOM DE COLONNE, JAMAIS PAR POSITION. Leçon payee le 13/07 :
        # j'ai reordonne le HEADER et la relecture par index a decale toutes les
        # saisies d'un cran. Le NOM est la source de verite, pas la place.
        idx: Dict[str, int] = {}
        for row in ws.get_all_values(value_render_option="UNFORMATTED_VALUE"):
            if "uuid" in row:                       # une ligne d'en-tete
                idx = {c: i for i, c in enumerate(row) if c}
                continue
            if not idx:
                continue
            uid = str(_case(row, idx, "uuid") or "").strip()
            if len(uid) < 30:
                continue
            saisi = {c: _case(row, idx, c) for c in MANUELLES}
            _nettoyer(saisi)
            if not any(str(v).strip() for v in saisi.values()):
                continue
            if not _saisie_coherente(saisi):
                rejetes += 1
                continue
            manuel[uid] = saisi
            repris += 1
        print(f"Saisies reprises de la page : {repris}.", flush=True)
        if rejetes:
            print(f"  ⚠️ {rejetes} lignes incoherentes ignorees -> reprises depuis "
                  f"🔗A-RACCORD.", flush=True)
    except gspread.WorksheetNotFound:
        print("🏆A-CLASSEMENT : creation.", flush=True)
    return manuel


def _case(row: List[Any], idx: Dict[str, int], col: str) -> Any:
    i = idx.get(col)
    return row[i] if (i is not None and i < len(row)) else ""


def _nettoyer(saisi: Dict[str, Any]) -> None:
    c = str(saisi.get("commentaire", "") or "").strip().upper()
    if c in ("TRUE", "FALSE", "VRAI", "FAUX"):      # residu d'un decalage de colonnes
        saisi["commentaire"] = ""


def _saisie_coherente(saisi: Dict[str, Any]) -> bool:
    """Une saisie qui ne ressemble pas a ce qu'elle devrait etre n'est pas reprise :
    on la reconstruit depuis l'amorce. C'est ce qui repare tout seul une page abimee."""
    v = str(saisi.get("valeur_irl_98", "") or "").strip()
    if v and _num(v) is None:
        return False
    n = str(saisi.get("note", "") or "").strip()
    if n and n not in ECHELLE:
        return False
    b = str(saisi.get("bonus_perso", "") or "").strip()
    if b and _num(b) is None:
        return False
    return True


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def etat_de(s: Dict[str, Any], m: Dict[str, Any], now: _dt.datetime,
            jours: int) -> str:
    """L'ordre des tests EST la regle metier :
       1. deja note                       -> plus rien a faire
       2. mercredi (comic) / AP (collect) -> Preda ne les note pas : ni module, ni alerte
       3. drop a venir                    -> 🔴 module
       4. drop des `jours` derniers jours -> 🆕 module
       5. le reste, non note              -> ⚠️ alerte
    """
    if (str(m.get("note", "") or "").strip()
            or str(m.get("valeur_irl_98", "") or "").strip()):
        return ""
    if s["type"] == "collectible" and s["edition_type"] == EDITION_NON_NOTEE:
        return ARTWORK
    d = s["date_drop"]
    if s["type"] == "comic" and d and d.weekday() == JOUR_VVBD:
        return VVBD
    if not d:
        return ALERTE
    if d > now:
        return A_VENIR
    if d >= now - _dt.timedelta(days=jours):
        return RECENT
    return ALERTE


def ligne(s: Dict[str, Any], m: Dict[str, Any], etat: str) -> List[Any]:
    est_comic = s["type"] == "comic"
    supply, gems = s["supply"], s["prix_gems"]
    floor, offres = s["floor"], s["offres"]
    valeur = _num(m.get("valeur_irl_98"))
    bonus = _num(m.get("bonus_perso")) or 0

    if est_comic:
        suggeree = note_comic(valeur, supply, bonus)
        score = score_comic(valeur, supply)
        par_edition = round(valeur / supply, 4) if (valeur and supply) else ""
        multiple = round(par_edition / gems, 2) if (par_edition != "" and gems) else ""
    else:
        suggeree = note_collectible(floor, offres, supply, s["edition_type"],
                                    s["veve_licensor"], bonus)
        score = score_collectible(floor, offres, supply, s["edition_type"],
                                  s["veve_licensor"])
        par_edition = ""
        multiple = (round(floor / gems, 2)
                    if (gems and floor_fiable(floor, offres)) else "")

    note = str(m.get("note", "") or "").strip()
    ecart = (ECHELLE.index(note) - ECHELLE.index(suggeree)
             if (note in ECHELLE and suggeree) else "")
    d = s["date_drop"]
    return [
        etat, s["type"], s["nom"], d.strftime("%d/%m/%Y %H:%M") if d else "",
        s["era"], supply or "", gems or "", s["edition_type"], s["cover_exclusive"],
        floor if floor and floor < FLOOR_MAX else "", offres if offres else "",
        m.get("valeur_irl_98", ""), m.get("fa_key", ""), m.get("bonus_perso", ""),
        note, suggeree, ecart, par_edition, multiple, score,
        m.get("commentaire", ""), s["rarity"], s["nb_raretes"], s["veve_brand"],
        s["veve_licensor"], s["start_year"], s["veve_url"], s["uuid"],
    ]


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERROR: SHEET_ID requis.", file=sys.stderr)
        return 2
    jours = int(os.environ.get("JOURS_RECENTS", "7"))
    now = _dt.datetime.utcnow()

    sh = _client().open_by_key(sheet_id)
    floors = load_floors(sh)
    items = load_comics(sh)
    items.update(load_collectibles(sh, floors))
    manuel = load_manuel(sh, set(items))

    vieille = _dt.datetime(1900, 1, 1)
    mod_c: List[Tuple[_dt.datetime, List[Any]]] = []
    mod_k: List[Tuple[_dt.datetime, List[Any]]] = []
    corps: List[Tuple[_dt.datetime, List[Any]]] = []
    for uid, s in items.items():
        m = manuel.get(uid, {})
        etat = etat_de(s, m, now, jours)
        l = ligne(s, m, etat)
        d = s["date_drop"] or vieille
        if etat in (A_VENIR, RECENT):
            (mod_c if s["type"] == "comic" else mod_k).append((d, l))
        else:
            corps.append((d, l))

    # ⚠️ on trie sur la VRAIE date : la colonne date_drop est du TEXTE, et trier
    # "31/12/2025" contre "01/01/2026" en texte donne le mauvais ordre.
    mod_c.sort(key=lambda x: x[0], reverse=True)
    mod_k.sort(key=lambda x: x[0], reverse=True)
    # le classement : les notes (par score), puis les ⚠️ a traiter, puis les 📅 et 🎨.
    # Trier par etat garde les blocs de couleur CONTIGUS -> une requete de format.
    rang = {"": 0, ALERTE: 1, VVBD: 2, ARTWORK: 3}
    corps.sort(key=lambda x: (
        rang.get(x[1][I["etat"]], 4),
        -(x[1][I["score"]] if isinstance(x[1][I["score"]], (int, float)) else -1),
        -x[0].timestamp(),
    ))
    module_c = [l for _, l in mod_c]
    module_k = [l for _, l in mod_k]
    body = [l for _, l in corps]

    # ----- assemblage -----
    grille: List[List[Any]] = []
    zones: List[Tuple[str, int, int]] = []       # (nature, 1re ligne, derniere)

    def bloc(titre: str, lignes: List[List[Any]], methode: str, nature: str) -> None:
        grille.append([titre] + [""] * (NB_COL - 1))
        zones.append(("titre", len(grille), len(grille)))
        grille.append(list(HEADER))
        zones.append(("entete", len(grille), len(grille)))
        debut = len(grille) + 1
        grille.extend(lignes or [["(rien à noter aujourd'hui)"] + [""] * (NB_COL - 1)])
        zones.append((nature, debut, len(grille)))
        if methode:
            grille.append([methode] + [""] * (NB_COL - 1))
            zones.append(("methode", len(grille), len(grille)))
            grille.append([""] * NB_COL)

    bloc(f"🆕 À NOTER — COMICS : {len(module_c)} (7 derniers jours + à venir, "
         f"hors mercredi)", module_c, _methode_comics(), "comics")
    bloc(f"🎨 À NOTER — COLLECTIBLES : {len(module_k)} (7 derniers jours + à venir, "
         f"hors Artist Proofs)", module_k, _methode_collectibles(), "collect")
    bloc(f"🏆 CLASSEMENT — {len(body)} lignes", body, "", "corps")

    try:
        ws = sh.worksheet(CLASSEMENT_TAB)
        ws.clear()
        try:
            ws.unmerge_cells(1, 1, ws.row_count, NB_COL)
        except Exception:
            pass
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=CLASSEMENT_TAB, rows=len(grille) + 100,
                              cols=NB_COL)
    if ws.row_count < len(grille) + 10:
        ws.add_rows(len(grille) + 10 - ws.row_count)
    if ws.col_count < NB_COL:
        ws.add_cols(NB_COL - ws.col_count)
    ws.update(range_name="A1", values=grille, value_input_option="RAW")

    # les 5 colonnes calculees en FORMULES : la note bouge pendant que Preda tape
    p, t = _col(I["note_suggeree"]), _col(I["score"])
    for nature, debut, fin in zones:
        if nature in ("comics", "collect", "corps") and (
                debut <= fin and grille[debut - 1][I["uuid"]]):
            ws.update(range_name=f"{p}{debut}:{t}{fin}",
                      values=[formules(r) for r in range(debut, fin + 1)],
                      value_input_option="USER_ENTERED")

    # --- habillage : UN SEUL batch (Google plafonne a 60 ecritures/minute) ---
    try:
        bandeau = {"backgroundColor": VIOLET, "horizontalAlignment": "LEFT",
                   "textFormat": {"bold": True, "fontSize": 12,
                                  "foregroundColor": BLANC}}
        entete = {"backgroundColor": FOND_ENTETE, "textFormat": {"bold": True},
                  "wrapStrategy": "WRAP"}
        methode = {"backgroundColor": FOND_METHODE, "wrapStrategy": "WRAP",
                   "verticalAlignment": "TOP",
                   "textFormat": {"italic": True, "fontSize": 9,
                                  "foregroundColor": GRIS}}
        fonds = {"comics": FOND_COMICS, "collect": FOND_COLLECT, "corps": BLANC}
        reqs: List[Dict[str, Any]] = []
        for nature, debut, fin in zones:
            if nature == "titre":
                reqs.append({"mergeCells": {"range": _plage(ws, debut, fin),
                                            "mergeType": "MERGE_ROWS"}})
                reqs.append(_peindre(ws, debut, fin, bandeau))
            elif nature == "entete":
                reqs.append(_peindre(ws, debut, fin, entete))
            elif nature == "methode":
                reqs.append({"mergeCells": {"range": _plage(ws, debut, fin),
                                            "mergeType": "MERGE_ROWS"}})
                reqs.append(_peindre(ws, debut, fin, methode))
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "ROWS",
                              "startIndex": debut - 1, "endIndex": fin},
                    "properties": {"pixelSize": 95}, "fields": "pixelSize"}})
            else:
                reqs.append(_peindre(ws, debut, fin,
                                     {"backgroundColor": fonds[nature]}))
                if nature == "corps":
                    alertes = [debut + i for i, l in enumerate(body)
                               if l[I["etat"]] == ALERTE]
                    for a, b in _blocs(alertes):
                        reqs.append(_peindre(ws, a, b,
                                             {"backgroundColor": FOND_ALERTE}))
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}})
        for i in range(0, len(reqs), 60):
            sh.batch_update({"requests": reqs[i:i + 60]})
        print(f"Habillage : {len(reqs)} requetes.", flush=True)
    except Exception as e:
        print(f"habillage : {e}", flush=True)

    compte = {e: sum(1 for l in body if l[I["etat"]] == e)
              for e in ("", ALERTE, VVBD, ARTWORK)}
    ecarts = [l[I["ecart"]] for l in body if isinstance(l[I["ecart"]], int)]
    print(f"\n🏆A-CLASSEMENT en {time.time() - t0:.0f}s")
    print(f"  a noter : {len(module_c)} comics + {len(module_k)} collectibles")
    print(f"  classement : {compte['']} notes · {compte[ALERTE]} ⚠️ a noter · "
          f"{compte[VVBD]} 📅 mercredi · {compte[ARTWORK]} 🎨 artist proofs")
    print(f"  ecart avec la note suggeree : {sum(1 for e in ecarts if e)} "
          f"(dont {sum(1 for e in ecarts if abs(e) >= 2)} de 2 crans ou +)")
    return 0


def _plage(ws, debut: int, fin: int) -> Dict[str, Any]:
    return {"sheetId": ws.id, "startRowIndex": debut - 1, "endRowIndex": fin,
            "startColumnIndex": 0, "endColumnIndex": NB_COL}


def _peindre(ws, debut: int, fin: int, fmt: Dict[str, Any]) -> Dict[str, Any]:
    champs = ",".join(f"userEnteredFormat.{k}" for k in fmt)
    return {"repeatCell": {"range": _plage(ws, debut, fin),
                           "cell": {"userEnteredFormat": fmt}, "fields": champs}}


def _blocs(lignes: List[int]):
    if not lignes:
        return
    lignes = sorted(lignes)
    debut = prec = lignes[0]
    for n in lignes[1:]:
        if n != prec + 1:
            yield debut, prec
            debut = n
        prec = n
    yield debut, prec


if __name__ == "__main__":
    raise SystemExit(main())
