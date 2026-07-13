"""
🏆A-CLASSEMENT — une ligne par SERIE comic VeVe. TOUT le catalogue (4 195 series).

LA PAGE EST EN BLOCS
    1. 🆕 A NOTER — les comics des 7 derniers jours + les drops a venir, PAS ENCORE
       notes et PAS du mercredi. C'est le module de travail : tout ce qu'il faut pour
       noter est sur la ligne. La note se calcule EN DIRECT (formules). Un comic sort
       du module des que sa valeur ou sa note est remplie.
    2. 🏆 CLASSEMENT — tout le reste : les notes en haut (tries par score), puis les
       ⚠️ non notes (fond rouge pale, qui s'efface des que tu remplis), puis les 📅
       du mercredi tout en bas.
    (3. un module COLLECTIBLES viendra ici — l'assemblage est deja fait par blocs.)

LE MERCREDI
    Preda ne note pas les comics du VeVe Comic Book Day (le mercredi) : ni dans le
    module, ni en alerte. Ils restent presents (etat 📅) mais ne reclament rien.
    C'est aussi ce qui rend le catalogue complet tenable : sans ca, 3 611 comics
    jamais notes auraient tous vire au rouge — et une alerte qui s'allume 3 611 fois
    n'est plus une alerte.

CE QUI EST A TOI (jamais ecrase)
    valeur_irl_98 · fa_key · bonus_perso · note · commentaire
    Relus par NOM DE COLONNE (jamais par position — leçon du 13/07) et revalides :
    une saisie qui ne ressemble pas a ce qu'elle devrait etre est rejetee et reprise
    depuis 🔗A-RACCORD. Une page abimee se repare donc toute seule.

LA GRILLE (arretee avec Preda le 13/07)
    note = bande de VALEUR IRL 9.8 (un cran par x3) DECALEE par le supply :
        < 1 000 -> +2 · 1 000-2 000 -> +1 · 2 000-5 000 -> 0 (normal)
        5 000-10 000 -> -1 · 10 000-15 000 -> -2 · > 15 000 -> -3
    + bonus_perso (± crans, a la main : le personnage vaut parfois plus que son comic)
    L'echelle de valeur n'est PAS plafonnee : le niveau peut depasser AAA avant que le
    supply ne le fasse redescendre. Sans ca, Amazing Fantasy #15 (5 M$, supply 10 000)
    ne pouvait mecaniquement plus etre AAA.
    Ancrages verifies : CA Comics #7 -> AA · ASM #14 Bouffon Vert -> BBB ·
    Fallen Son -> C · Amazing Fantasy #15 -> AAA.

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, JOURS_RECENTS (7)
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COMICS_TAB = "🟢C-COMICS"
RACCORD_TAB = "🔗A-RACCORD"
CLASSEMENT_TAB = "🏆A-CLASSEMENT"

# ---------------------------------------------------------------------------
# LA GRILLE — les 3 constantes a bouger pour durcir / adoucir
# ---------------------------------------------------------------------------
ECHELLE = ["C", "CC", "CCC", "B", "BB", "BBB", "A", "AA", "AAA"]
BASE_VALEUR = 35.0        # frontiere C / CC a supply neutre
RATIO = 3.0               # un cran de note = x3 sur la valeur IRL
PALIERS_SUPPLY = [        # (supply STRICTEMENT INFERIEUR A, crans)
    (1000, +2),           # tres avantage
    (2001, +1),           # avantage
    (5001, 0),            # normal
    (10001, -1),          # penalisant (7 500 est ici)
    (15001, -2),
    (float("inf"), -3),   # tres penalisant
]

JOUR_VVBD = 2             # mercredi (lundi = 0) : le VeVe Comic Book Day

MANUELLES = ["valeur_irl_98", "fa_key", "bonus_perso", "note", "commentaire"]

HEADER = [
    "etat", "veve_series_name", "date_drop", "era", "supply", "prix_gems",
    "cover_exclusive", "valeur_irl_98", "fa_key", "bonus_perso", "note",
    "note_suggeree", "ecart", "valeur_par_edition", "multiple_entree", "score",
    "commentaire", "nb_raretes", "veve_brand", "veve_licensor", "start_year",
    "veve_url", "series_uuid",
]
I = {c: i for i, c in enumerate(HEADER)}
NB_COL = len(HEADER)
DERNIERE = chr(ord("A") + NB_COL - 1)          # W

A_VENIR, RECENT, ALERTE, VVBD = "🔴", "🆕", "⚠️", "📅"

VIOLET = {"red": 0.42, "green": 0.35, "blue": 0.66}
BLANC = {"red": 1.0, "green": 1.0, "blue": 1.0}
FOND_MODULE = {"red": 1.0, "green": 0.94, "blue": 0.85}
FOND_ENTETE = {"red": 0.91, "green": 0.88, "blue": 0.96}
FOND_METHODE = {"red": 0.96, "green": 0.96, "blue": 0.96}
FOND_ALERTE = {"red": 0.99, "green": 0.89, "blue": 0.89}
GRIS = {"red": 0.40, "green": 0.40, "blue": 0.40}

URL_COMIC = "https://www.veve.me/collectibles/en/comics/{uuid}"
_EPOCH = _dt.datetime(1899, 12, 30)             # jour 0 des dates Google Sheets


def _methode() -> str:
    b = [BASE_VALEUR * (RATIO ** i) for i in range(8)]
    bandes = " · ".join(f"{n} {v:,.0f}$".replace(",", " ")
                        for n, v in zip(ECHELLE[1:], b))
    return (
        "MÉTHODE — la note part de la VALEUR IRL du comic physique en 9.8 "
        f"(un cran par ×{RATIO:.0f}), puis le SUPPLY la décale : "
        "moins de 1 000 → +2 crans · 1 000-2 000 → +1 · 2 000-5 000 → 0 (normal) · "
        "5 000-10 000 → −1 · 10 000-15 000 → −2 · plus de 15 000 → −3.    "
        f"Bandes à supply neutre : C sous {BASE_VALEUR:,.0f}$ · {bandes}.    "
        "À REMPLIR : valeur_irl_98 (prix du 9.8 physique), fa_key (la première "
        "apparition / le fait marquant), bonus_perso (± crans quand le personnage "
        "vaut plus que son comic : Venom +2, Miles Morales +2), puis note. "
        "La note suggérée se recalcule pendant que tu tapes. Le comic quitte ce "
        "module dès que sa valeur ou sa note est remplie. "
        "Les comics du mercredi (VeVe Comic Book Day) n'entrent jamais ici."
    ).replace(",", " ")


# ---------------------------------------------------------------------------
# Grille
# ---------------------------------------------------------------------------

def crans_supply(supply: Optional[float]) -> int:
    if not supply or supply <= 0:
        return 0
    for borne, crans in PALIERS_SUPPLY:
        if supply < borne:
            return crans
    return PALIERS_SUPPLY[-1][1]


def niveau_valeur(valeur: float) -> int:
    """NON PLAFONNE : le niveau peut depasser AAA, c'est le supply qui redescend."""
    if valeur < BASE_VALEUR:
        return 0
    return int(math.floor(math.log(valeur / BASE_VALEUR) / math.log(RATIO))) + 1


def note_calculee(valeur: Optional[float], supply: Optional[float],
                  bonus: Optional[float]) -> str:
    if not valeur or not supply or supply <= 0:
        return ""
    k = niveau_valeur(valeur) + crans_supply(supply) + int(bonus or 0)
    return ECHELLE[max(0, min(len(ECHELLE) - 1, k))]


def score_brut(valeur: Optional[float], supply: Optional[float]) -> Any:
    if not valeur or not supply or supply <= 0:
        return ""
    return round(valeur * (RATIO ** crans_supply(supply)), 1)


# --- les memes formules, mais VIVANTES dans le Sheet -----------------------
# La note se recalcule pendant que Preda tape. Elles sont GENEREES depuis les
# constantes ci-dessus : la grille du Sheet ne peut pas diverger de celle du Python.
# ⚠️ Locale FR : separateur « ; » (une virgule donne #ERROR!).

def _cascade_supply(col: str) -> str:
    bornes = [(b, c) for b, c in PALIERS_SUPPLY if b != float("inf")]
    f = str(PALIERS_SUPPLY[-1][1])
    for borne, crans in reversed(bornes):
        f = f"IF({col}<{borne:.0f};{crans};{f})"
    return f


def _niveau_formule(col: str) -> str:
    return (f"IF({col}<{BASE_VALEUR:.0f};0;"
            f"FLOOR(LN({col}/{BASE_VALEUR:.0f})/LN({RATIO:.0f}))+1)")


def formules(r: int) -> List[str]:
    """Les 5 colonnes calculees de la ligne r (L a P)."""
    h, e, j, k, n, f, l = (f"$H{r}", f"$E{r}", f"$J{r}", f"$K{r}",
                           f"$N{r}", f"$F{r}", f"$L{r}")
    ech = "{" + ";".join(f'"{x}"' for x in ECHELLE) + "}"
    vide = f'OR({h}="";{e}="")'
    cran = (f"MIN({len(ECHELLE)};MAX(1;1+{_niveau_formule(h)}"
            f"+{_cascade_supply(e)}+IF({j}=\"\";0;{j})))")
    return [
        f'=IF({vide};"";INDEX({ech};{cran}))',
        f'=IF(OR({k}="";{l}="");"";MATCH({k};{ech};0)-MATCH({l};{ech};0))',
        f'=IF({vide};"";{h}/{e})',
        f'=IF(OR({n}="";{f}="");"";{n}/{f})',
        f'=IF({vide};"";{h}*POWER({RATIO:.0f};{_cascade_supply(e)}))',
    ]


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


def prix_en_gems(x: Any) -> Optional[float]:
    """Prix boutique d'un COMIC ramene en gems (1 gem ~ 1 $).

    VeVe melange DEUX echelles dans `storePrice` : vieux comics en GEMS (10, 15, 20),
    comics recents en FIAT et en CENTIMES (699, 798, 1499). Preuve : Captain America
    Comics #7 = 699 au catalogue et la carte Discord de Preda dit « 7 gems ».
    Au-dela de 100 = centimes. IDEMPOTENT. ⚠️ Comics seulement (un collectible a
    1 500 gems, ça existe).
    """
    v = _num(x)
    if v is None:
        return None
    return round(v / 100, 2) if v >= 100 else v


def _date(x: Any) -> Optional[_dt.datetime]:
    """⚠️ Le catalogue est lu en UNFORMATTED (obligatoire : locale FR, "6,99" serait
    numerise en 699). Dans ce mode une cellule DATE revient en NUMERO DE SERIE
    Google (46 212,625), pas en texte."""
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
    if annee < 1956:
        return "Golden"
    if annee < 1970:
        return "Silver"
    if annee < 1985:
        return "Bronze"
    if annee < 1992:
        return "Copper"
    return "Modern"


def _client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON manquant.")
    return gspread.authorize(
        Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES))


def load_series(sh) -> Dict[str, Dict[str, Any]]:
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
                "series_uuid": uid,
                "veve_series_name": r.get("veve_series_name", ""),
                "date_drop": _date(r.get("releaseDate")),
                "supply": _num(r.get("supply")),
                "prix_gems": prix_en_gems(r.get("store_price_gems")),
                "cover_exclusive": r.get("veve_exclusive", ""),
                "veve_brand": r.get("veve_brand", ""),
                "veve_licensor": r.get("veve_licensor", ""),
                "start_year": int(annee) if annee else "",
                "veve_url": URL_COMIC.format(uuid=uid),
                "nb_raretes": 0,
            }
        s["nb_raretes"] += 1

    mercredi = sum(1 for s in out.values()
                   if s["date_drop"] and s["date_drop"].weekday() == JOUR_VVBD)
    dernier = max((s["date_drop"] for s in out.values() if s["date_drop"]), default=None)
    print(f"Catalogue : {len(out)} series comics.", flush=True)
    print(f"  sans supply : {sum(1 for s in out.values() if not s['supply'])} "
          f"(fiches delistees chez VeVe)", flush=True)
    print(f"  du mercredi (VVBD, jamais a noter) : {mercredi}", flush=True)
    print(f"  drop le plus recent connu : "
          f"{dernier.strftime('%d/%m/%Y %H:%M') if dernier else '-'}", flush=True)
    return out


def load_manuel(sh, series: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Les saisies : la PAGE d'abord (c'est la que Preda tape), 🔗A-RACCORD ensuite
    pour amorcer ce qui manque."""
    manuel: Dict[str, Dict[str, Any]] = {}

    try:
        for r in sh.worksheet(RACCORD_TAB).get_all_records(
                value_render_option="UNFORMATTED_VALUE"):
            valide = str(r.get("valide", "") or "").strip()
            uid = (valide if len(valide) > 10
                   else str(r.get("series_uuid", "") or "").strip())
            if not uid or uid not in series:
                continue
            if str(r.get("statut", "")).strip() != "CERTAIN" and not valide:
                continue
            manuel.setdefault(uid, {
                "valeur_irl_98": r.get("valeur_irl", ""),
                "fa_key": r.get("fa_preda", ""),
                "bonus_perso": "",
                "note": r.get("note", ""),
                "commentaire": "",
            })
        print(f"Amorce depuis le classement : {len(manuel)} series.", flush=True)
    except gspread.WorksheetNotFound:
        print("🔗A-RACCORD absent : aucune amorce.", flush=True)

    try:
        ws = sh.worksheet(CLASSEMENT_TAB)
        repris, rejetes = 0, 0
        # ⚠️ LECTURE PAR NOM DE COLONNE, JAMAIS PAR POSITION. Leçon payee le 13/07 :
        # j'ai reordonne le HEADER et la relecture par index a decale toutes les
        # saisies d'un cran (valeur_irl_98 a herite de `note`, `note` de
        # `valeur_par_edition`...). Le NOM est la source de verite, pas la place.
        idx: Dict[str, int] = {}
        for row in ws.get_all_values(value_render_option="UNFORMATTED_VALUE"):
            if "series_uuid" in row:               # ligne d'en-tete (il y en a 2)
                idx = {c: i for i, c in enumerate(row) if c}
                continue
            if not idx:
                continue
            uid = str(_case(row, idx, "series_uuid") or "").strip()
            if len(uid) < 30:
                continue
            saisi = {c: _case(row, idx, c) for c in MANUELLES}
            _nettoyer(saisi)
            if not any(str(v).strip() for v in saisi.values()):
                continue
            if not _saisie_coherente(saisi):
                rejetes += 1                       # abimee -> on repart de l'amorce
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
    """`commentaire` avait herite d'un TRUE/FALSE lors du decalage de colonnes du
    13/07. Un booleen n'est pas un commentaire."""
    c = str(saisi.get("commentaire", "") or "").strip().upper()
    if c in ("TRUE", "FALSE", "VRAI", "FAUX"):
        saisi["commentaire"] = ""


def _saisie_coherente(saisi: Dict[str, Any]) -> bool:
    """Garde-fou : une saisie qui ne ressemble pas a ce qu'elle devrait etre (valeur
    en dollars, note de l'echelle, bonus entier) n'est pas reprise — on la reconstruit
    depuis l'amorce. C'est ce qui repare tout seul une page abimee."""
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
    """L'etat d'une serie. L'ordre des tests EST la regle metier :
       1. notee               -> plus rien a faire
       2. mercredi (VVBD)     -> Preda ne les note pas : ni module, ni alerte
       3. drop a venir        -> 🔴 module (a noter AVANT la carte Discord)
       4. drop des 7 derniers jours -> 🆕 module
       5. le reste, non note  -> ⚠️ alerte
    """
    if str(m.get("note", "") or "").strip() or str(m.get("valeur_irl_98", "") or "").strip():
        return ""
    d = s["date_drop"]
    if d and d.weekday() == JOUR_VVBD:
        return VVBD
    if not d:
        return ALERTE
    if d > now:
        return A_VENIR
    if d >= now - _dt.timedelta(days=jours):
        return RECENT
    return ALERTE


def ligne(s: Dict[str, Any], m: Dict[str, Any], etat: str) -> List[Any]:
    valeur = _num(m.get("valeur_irl_98"))
    supply, gems, d = s["supply"], s["prix_gems"], s["date_drop"]
    bonus = _num(m.get("bonus_perso")) or 0

    par_edition = round(valeur / supply, 4) if (valeur and supply) else ""
    multiple = round(par_edition / gems, 2) if (par_edition != "" and gems) else ""
    suggeree = note_calculee(valeur, supply, bonus)
    note = str(m.get("note", "") or "").strip()
    ecart = (ECHELLE.index(note) - ECHELLE.index(suggeree)
             if (note in ECHELLE and suggeree) else "")

    return [
        etat, s["veve_series_name"], d.strftime("%d/%m/%Y %H:%M") if d else "",
        _era(s["start_year"] or None), supply or "", gems or "",
        s["cover_exclusive"],
        m.get("valeur_irl_98", ""), m.get("fa_key", ""), m.get("bonus_perso", ""),
        note, suggeree, ecart, par_edition, multiple, score_brut(valeur, supply),
        m.get("commentaire", ""), s["nb_raretes"], s["veve_brand"],
        s["veve_licensor"], s["start_year"], s["veve_url"], s["series_uuid"],
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
    series = load_series(sh)
    manuel = load_manuel(sh, series)

    # PERIMETRE : TOUT le catalogue. Star Wars, Tarzan, Disney, VIZ, 247 Comics
    # etaient bien collectes — c'est la page qui ne les laissait pas entrer.
    # ⚠️ on garde la VRAIE date a cote de la ligne pour trier. La colonne date_drop
    # est du TEXTE ("09/07/2026 15:00") : trier dessus, c'est trier "31/12/2025"
    # apres "01/01/2026". Une date formatee n'est pas une date.
    mod: List[Tuple[_dt.datetime, List[Any]]] = []
    cor: List[Tuple[Any, List[Any]]] = []
    vieille = _dt.datetime(1900, 1, 1)
    for uid, s in series.items():
        m = manuel.get(uid, {})
        etat = etat_de(s, m, now, jours)
        l = ligne(s, m, etat)
        (mod if etat in (A_VENIR, RECENT) else cor).append((s["date_drop"] or vieille, l))

    # le module : le plus imminent / le plus recent en tete
    mod.sort(key=lambda x: x[0], reverse=True)
    module = [l for _, l in mod]

    # le classement : les notes d'abord (par score), puis les ⚠️ a traiter, puis les
    # 📅 du mercredi. Trier par etat garde aussi les blocs de couleur CONTIGUS —
    # une seule requete de format au lieu de centaines.
    rang = {"": 0, ALERTE: 1, VVBD: 2}
    cor.sort(key=lambda x: (
        rang.get(x[1][I["etat"]], 3),
        -(x[1][I["score"]] if isinstance(x[1][I["score"]], (int, float)) else -1),
        -x[0].timestamp(),
    ))
    corps = [l for _, l in cor]

    # ----- assemblage par BLOCS (le module COLLECTIBLES se branchera ici) -----
    titre_mod = (f"🆕 À NOTER — {len(module)} comic(s) : les {jours} derniers jours "
                 f"+ les drops à venir (hors mercredi)")
    grille: List[List[Any]] = [[titre_mod] + [""] * (NB_COL - 1), list(HEADER)]
    grille += module or [["(rien à noter aujourd'hui)"] + [""] * (NB_COL - 1)]
    grille.append([_methode()] + [""] * (NB_COL - 1))
    grille.append([""] * NB_COL)
    grille.append([f"🏆 CLASSEMENT — {len(corps)} séries"] + [""] * (NB_COL - 1))
    grille.append(list(HEADER))
    grille += corps

    n_mod = max(len(module), 1)
    L_TITRE_MOD, L_ENTETE_MOD, L_MODULE = 1, 2, 3
    L_METHODE = L_MODULE + n_mod
    L_TITRE_CLASS = L_METHODE + 2
    L_ENTETE_CLASS = L_TITRE_CLASS + 1
    L_CORPS = L_ENTETE_CLASS + 1

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
    ws.update(range_name="A1", values=grille, value_input_option="RAW")

    # les 5 colonnes calculees en FORMULES : la note bouge pendant que Preda tape.
    for debut, nb in ((L_MODULE, len(module)), (L_CORPS, len(corps))):
        if nb:
            ws.update(range_name=f"L{debut}:P{debut + nb - 1}",
                      values=[formules(debut + i) for i in range(nb)],
                      value_input_option="USER_ENTERED")

    # --- habillage : UN SEUL batch (Google plafonne a 60 ecritures/minute, le 1er
    # run est tombe en 429 avec un appel par bloc de couleur). Un batch est ATOMIQUE
    # -> paquets de 60, un accident reste local.
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
        reqs: List[Dict[str, Any]] = []
        for l in (L_TITRE_MOD, L_METHODE, L_TITRE_CLASS):
            reqs.append({"mergeCells": {"range": _plage(ws, l, l),
                                        "mergeType": "MERGE_ROWS"}})
        for l, fmt in ((L_TITRE_MOD, bandeau), (L_TITRE_CLASS, bandeau),
                       (L_ENTETE_MOD, entete), (L_ENTETE_CLASS, entete),
                       (L_METHODE, methode)):
            reqs.append(_peindre(ws, l, l, fmt))
        reqs.append(_peindre(ws, L_MODULE, L_MODULE + n_mod - 1,
                             {"backgroundColor": FOND_MODULE}))
        if corps:
            reqs.append(_peindre(ws, L_CORPS, L_CORPS + len(corps) - 1,
                                 {"backgroundColor": BLANC}))
            alertes = [L_CORPS + i for i, l in enumerate(corps)
                       if l[I["etat"]] == ALERTE]
            for debut, fin in _blocs(alertes):
                reqs.append(_peindre(ws, debut, fin,
                                     {"backgroundColor": FOND_ALERTE}))
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "ROWS",
                      "startIndex": L_METHODE - 1, "endIndex": L_METHODE},
            "properties": {"pixelSize": 90}, "fields": "pixelSize"}})
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": ws.id,
                           "gridProperties": {"frozenRowCount": L_ENTETE_MOD}},
            "fields": "gridProperties.frozenRowCount"}})
        for i in range(0, len(reqs), 60):
            sh.batch_update({"requests": reqs[i:i + 60]})
        print(f"Habillage : {len(reqs)} requetes.", flush=True)
    except Exception as e:
        print(f"habillage : {e}", flush=True)

    ecarts = [l[I["ecart"]] for l in corps if isinstance(l[I["ecart"]], int)]
    compte = {e: sum(1 for l in corps if l[I["etat"]] == e) for e in ("", ALERTE, VVBD)}
    print(f"\n🏆A-CLASSEMENT : {len(module)} a noter + {len(corps)} au classement "
          f"en {time.time() - t0:.0f}s")
    print(f"  module : {sum(1 for l in module if l[I['etat']] == A_VENIR)} a venir + "
          f"{sum(1 for l in module if l[I['etat']] == RECENT)} recents")
    print(f"  classement : {compte['']} notees · {compte[ALERTE]} ⚠️ a noter · "
          f"{compte[VVBD]} 📅 mercredi (ignores)")
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
    """Numeros de ligne consecutifs -> (debut, fin). Une requete de format par bloc."""
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
