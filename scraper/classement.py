"""
🏆A-CLASSEMENT — une ligne par SERIE comic VeVe.

LA PAGE EST EN DEUX BLOCS
    1. 🆕 A NOTER — les comics des 7 derniers jours (+ ceux a venir) qui n'ont pas
       encore de note. C'est le module de travail : tout ce qu'il faut pour noter
       est sur la ligne, et rien d'autre a chercher. Une note de methode dessous.
    2. 🏆 CLASSEMENT — tout le reste, trie par score. Les anciens non notes gardent
       un fond d'alerte qui DISPARAIT des que tu remplis (certains ne seront jamais
       notables : ils resteront en alerte, c'est sans gravite).

CE QUI EST A TOI (jamais ecrase)
    valeur_irl_98 · fa_key · bonus_perso · note · commentaire
    Relus dans les DEUX blocs a chaque run (la cle est le series_uuid en derniere
    colonne) et reecrits tels quels.

LA GRILLE (arretee avec Preda le 13/07)
    note = bande de VALEUR IRL 9.8 (un cran par x3) DECALEE par le supply :

        supply  < 1 000   -> +2 crans   (tres avantage)
        supply 1 000-2 000 -> +1
        supply 2 000-5 000 ->  0        (le supply "normal")
        supply 5 000-10 000 -> -1       (penalisant)
        supply 10 000-15 000 -> -2
        supply > 15 000    -> -3        (tres penalisant)

    L'echelle de valeur n'est PAS plafonnee : le niveau peut depasser AAA avant que
    le supply ne le fasse redescendre. Sans ca, Amazing Fantasy #15 (5 M$, supply
    10 000) ne pouvait mecaniquement PLUS etre AAA — le malus l'aurait bloque a AA.

    Verifie sur les ancrages de Preda : ASM #14 Bouffon Vert (210 k$/70 000) -> BBB ·
    Fallen Son (~200 $/30 000) -> C · Captain America Comics #7 (25 k$/1 000) -> AA ·
    Amazing Fantasy #15 -> AAA.

    ⚠️ Cette grille contredit 89 des 405 notes historiques de 2 crans ou plus (dont
    Sgt. Fury #1, 76 k$ / supply 30 000 : A -> BBB). C'est VOULU : elle traduit la
    regle que Preda veut appliquer, pas ses notes passees. La colonne `ecart` montre
    ou les deux divergent, et `note` reste souveraine.

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, JOURS_RECENTS (defaut 7)
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COMICS_TAB = "🟢C-COMICS"
RACCORD_TAB = "🔗A-RACCORD"
CLASSEMENT_TAB = "🏆A-CLASSEMENT"

# ---------------------------------------------------------------------------
# LA GRILLE — les 3 constantes a bouger si Preda veut durcir / adoucir
# ---------------------------------------------------------------------------
ECHELLE = ["C", "CC", "CCC", "B", "BB", "BBB", "A", "AA", "AAA"]
BASE_VALEUR = 35.0        # frontiere C / CC a supply neutre
RATIO = 3.0               # un cran de note = x3 sur la valeur IRL
PALIERS_SUPPLY = [        # (supply STRICTEMENT INFERIEUR A, crans) — 1er palier gagnant
    (1000, +2),           # moins de 1 000 : tres avantage
    (2001, +1),           # 1 000 a 2 000  : avantage
    (5001, 0),            # 2 000 a 5 000  : le supply "normal"
    (10001, -1),          # 5 000 a 10 000 : penalisant (7 500 est ici)
    (15001, -2),          # 10 000 a 15 000
    (float("inf"), -3),   # plus de 15 000 : tres penalisant
]

MANUELLES = ["valeur_irl_98", "fa_key", "bonus_perso", "note", "commentaire"]

# Les colonnes de SAISIE viennent tot : le module "a noter" doit se lire sans
# scroller. Le reste (diagnostic, references) suit.
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

A_VENIR, RECENT, ALERTE = "🔴", "🆕", "⚠️"

# --- habillage -------------------------------------------------------------
VIOLET = {"red": 0.42, "green": 0.35, "blue": 0.66}     # bandeaux (texte blanc)
BLANC = {"red": 1.0, "green": 1.0, "blue": 1.0}
FOND_MODULE = {"red": 1.0, "green": 0.94, "blue": 0.85}  # orange tres clair
FOND_ENTETE = {"red": 0.91, "green": 0.88, "blue": 0.96}
FOND_METHODE = {"red": 0.96, "green": 0.96, "blue": 0.96}
FOND_ALERTE = {"red": 0.99, "green": 0.89, "blue": 0.89}  # rouge tres clair
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
        "La note suggérée s'affiche à côté — elle ne remplace jamais la tienne, "
        "la colonne `ecart` montre juste où vous divergez."
    ).replace(",", " ")


# ---------------------------------------------------------------------------
# Grille
# ---------------------------------------------------------------------------

def crans_supply(supply: Optional[float]) -> int:
    """Le decalage en crans du au tirage. Bornes STRICTES : un supply de 1 000 pile
    est « avantage » (+1), pas « tres avantage » — c'est ce qui met Captain America
    Comics #7 (25 k$, supply 1 000) sur AA, la note que Preda lui a donnee."""
    if not supply or supply <= 0:
        return 0
    for borne, crans in PALIERS_SUPPLY:
        if supply < borne:
            return crans
    return PALIERS_SUPPLY[-1][1]


def niveau_valeur(valeur: float) -> int:
    """Le cran de valeur, NON PLAFONNE (il peut depasser AAA — c'est le supply qui
    redescend ensuite). Sans ca, un 5 M$ a supply 10 000 ne pouvait plus etre AAA."""
    if valeur < BASE_VALEUR:
        return 0
    return int(math.floor(math.log(valeur / BASE_VALEUR) / math.log(RATIO))) + 1


def note_calculee(valeur: Optional[float], supply: Optional[float],
                  bonus: Optional[float]) -> str:
    if not valeur or not supply or supply <= 0:
        return ""
    k = niveau_valeur(valeur) + crans_supply(supply) + int(bonus or 0)
    return ECHELLE[max(0, min(len(ECHELLE) - 1, k))]


# ---------------------------------------------------------------------------
# LES MEMES FORMULES, EN VIVANT DANS LE SHEET
# ---------------------------------------------------------------------------
# note_suggeree / ecart / valeur_par_edition / multiple / score sont poses en
# FORMULES, pas en valeurs : la note se recalcule pendant que Preda tape sa valeur
# IRL ou son bonus, sans attendre le run du lendemain. C'est tout l'interet du
# module « a noter ».
# Les formules sont GENEREES a partir des constantes ci-dessus — impossible que la
# grille du Sheet et celle du Python divergent.
# ⚠️ Locale FR : le separateur d'arguments est « ; » (une virgule donne #ERROR!).

def _cascade_supply(col: str) -> str:
    """IF(E<1000;2;IF(E<2001;1;...)) construit depuis PALIERS_SUPPLY."""
    bornes = [(b, c) for b, c in PALIERS_SUPPLY if b != float("inf")]
    defaut = PALIERS_SUPPLY[-1][1]
    f = str(defaut)
    for borne, crans in reversed(bornes):
        f = f"IF({col}<{borne:.0f};{crans};{f})"
    return f


def _niveau_valeur(col: str) -> str:
    return (f"IF({col}<{BASE_VALEUR:.0f};0;"
            f"FLOOR(LN({col}/{BASE_VALEUR:.0f})/LN({RATIO:.0f}))+1)")


def formules(r: int) -> List[str]:
    """Les 5 colonnes calculees de la ligne r (L a P), dans l'ordre du HEADER."""
    h, e, j, k, n, f, l = (f"$H{r}", f"$E{r}", f"$J{r}", f"$K{r}",
                           f"$N{r}", f"$F{r}", f"$L{r}")
    ech = "{" + ";".join(f'"{x}"' for x in ECHELLE) + "}"
    vide = f'OR({h}="";{e}="")'
    cran = (f"MIN({len(ECHELLE)};MAX(1;1+{_niveau_valeur(h)}"
            f"+{_cascade_supply(e)}+IF({j}=\"\";0;{j})))")
    return [
        f'=IF({vide};"";INDEX({ech};{cran}))',                       # note_suggeree
        f'=IF(OR({k}="";{l}="");"";MATCH({k};{ech};0)-MATCH({l};{ech};0))',  # ecart
        f'=IF({vide};"";{h}/{e})',                                   # valeur_par_edition
        f'=IF(OR({n}="";{f}="");"";{n}/{f})',                        # multiple_entree
        f'=IF({vide};"";{h}*POWER({RATIO:.0f};{_cascade_supply(e)}))',       # score
    ]


def score_brut(valeur: Optional[float], supply: Optional[float]) -> Any:
    """Un reel pour trier / departager, dans la MEME logique que la grille :
    la valeur, corrigee des crans de supply (1 cran = x3)."""
    if not valeur or not supply or supply <= 0:
        return ""
    return round(valeur * (RATIO ** crans_supply(supply)), 1)


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
    """Le prix boutique d'un COMIC ramene en gems (1 gem ~ 1 $).

    VeVe melange DEUX echelles dans `storePrice` : les vieux comics etaient vendus
    en GEMS (10, 15, 20), les recents en FIAT et en CENTIMES (699, 798, 1499).
    Preuve : Captain America Comics #7 = 699 au catalogue, et la carte Discord de
    Preda dit « 7 gems » ; sur Cheetara #2 le tracker dit 7.98 quand GraphQL dit 798.
    Au-dela de 100 = centimes. IDEMPOTENT (7,98 < 100 : pas de 2e division).
    ⚠️ Comics seulement — un collectible a 1 500 gems, ça existe.
    """
    v = _num(x)
    if v is None:
        return None
    return round(v / 100, 2) if v >= 100 else v


def _date(x: Any) -> Optional[_dt.datetime]:
    """⚠️ On lit le catalogue en UNFORMATTED (obligatoire : locale FR, "6,99" serait
    numerise en 699). Dans ce mode une cellule DATE revient en NUMERO DE SERIE
    Google (46 212,625), pas en texte. On reconvertit."""
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

    sans_supply = sum(1 for s in out.values() if not s["supply"])
    sans_prix = sum(1 for s in out.values() if not s["prix_gems"])
    dernier = max((s["date_drop"] for s in out.values() if s["date_drop"]), default=None)
    print(f"Catalogue : {len(out)} series comics.", flush=True)
    print(f"  sans supply : {sans_supply} (fiches delistees chez VeVe)", flush=True)
    print(f"  sans prix   : {sans_prix}", flush=True)
    print(f"  drop le plus recent connu : "
          f"{dernier.strftime('%d/%m/%Y %H:%M') if dernier else '-'}", flush=True)
    return out


def load_manuel(sh, series: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Les colonnes manuelles. La PAGE d'abord (c'est la que Preda saisit), le
    classement historique ensuite pour amorcer ce qui manque.

    La page a deux blocs mais UNE SEULE grille de colonnes : on repere les lignes de
    donnees a leur derniere cellule (le series_uuid). Pas de dependance a un numero
    de ligne — le module du haut change de taille tous les jours.
    """
    manuel: Dict[str, Dict[str, Any]] = {}

    try:
        for r in sh.worksheet(RACCORD_TAB).get_all_records(
                value_render_option="UNFORMATTED_VALUE"):
            valide = str(r.get("valide", "") or "").strip()
            uid = valide if len(valide) > 10 else str(r.get("series_uuid", "") or "").strip()
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
        repris = 0
        for row in ws.get_all_values(value_render_option="UNFORMATTED_VALUE"):
            if len(row) < NB_COL:
                continue
            uid = str(row[I["series_uuid"]] or "").strip()
            if len(uid) < 30:                      # pas une ligne de donnees
                continue
            saisi = {c: row[I[c]] for c in MANUELLES}
            if any(str(v).strip() for v in saisi.values()):
                manuel[uid] = saisi
                repris += 1
        print(f"Saisies reprises de la page : {repris} series.", flush=True)
    except gspread.WorksheetNotFound:
        print("🏆A-CLASSEMENT : creation.", flush=True)

    return manuel


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

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

    def est_note(uid: str) -> bool:
        m = manuel.get(uid) or {}
        return bool(str(m.get("note", "") or "").strip()
                    or str(m.get("valeur_irl_98", "") or "").strip())

    # --- BLOC 1 : a noter (a venir + 7 derniers jours, sans note) ---
    a_noter: List[List[Any]] = []
    for uid, s in series.items():
        d = s["date_drop"]
        if not d or est_note(uid):
            continue
        if d > now:
            a_noter.append((d, ligne(s, manuel.get(uid, {}), A_VENIR)))
        elif d >= now - _dt.timedelta(days=jours):
            a_noter.append((d, ligne(s, manuel.get(uid, {}), RECENT)))
    a_noter.sort(key=lambda x: x[0], reverse=True)   # le plus recent / imminent en tete
    module = [l for _, l in a_noter]
    dans_module = {l[I["series_uuid"]] for l in module}

    # --- BLOC 2 : le classement (tout ce qui a une saisie + le reste des notees) ---
    corps: List[List[Any]] = []
    for uid in set(manuel):
        if uid in series and uid not in dans_module:
            etat = "" if est_note(uid) else ALERTE
            corps.append(ligne(series[uid], manuel[uid], etat))
    corps.sort(key=lambda l: -(l[I["score"]] if isinstance(l[I["score"]], (int, float))
                               else -1))

    # --- ecriture ---
    titre_module = (f"🆕 À NOTER — {len(module)} comic(s) : les {jours} derniers jours "
                    f"+ les drops à venir")
    grille: List[List[Any]] = [[titre_module] + [""] * (NB_COL - 1), list(HEADER)]
    grille += module or [["(rien à noter aujourd'hui)"] + [""] * (NB_COL - 1)]
    grille.append([_methode()] + [""] * (NB_COL - 1))
    grille.append([""] * NB_COL)
    grille.append([f"🏆 CLASSEMENT — {len(corps)} séries"] + [""] * (NB_COL - 1))
    grille.append(list(HEADER))
    grille += corps

    n_mod = max(len(module), 1)
    L_TITRE_MOD, L_ENTETE_MOD = 1, 2
    L_MODULE = 3                                  # 1re ligne de donnees du module
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

    # Les 5 colonnes calculees (L a P) sont posees en FORMULES : la note suggeree
    # se met a jour PENDANT que Preda tape, sans attendre le run du lendemain.
    # USER_ENTERED (sinon la formule serait ecrite comme du texte) et un seul appel
    # par bloc — les bandeaux fusionnes entre les deux blocs ne doivent pas etre
    # touches.
    for debut, nb in ((L_MODULE, len(module)), (L_CORPS, len(corps))):
        if nb:
            ws.update(range_name=f"L{debut}:P{debut + nb - 1}",
                      values=[formules(debut + i) for i in range(nb)],
                      value_input_option="USER_ENTERED")

    # --- habillage ---
    try:
        for ligne_no in (L_TITRE_MOD, L_METHODE, L_TITRE_CLASS):
            ws.merge_cells(f"A{ligne_no}:{DERNIERE}{ligne_no}")

        bandeau = {"backgroundColor": VIOLET, "horizontalAlignment": "LEFT",
                   "textFormat": {"bold": True, "fontSize": 12,
                                  "foregroundColor": BLANC}}
        entete = {"backgroundColor": FOND_ENTETE,
                  "textFormat": {"bold": True}, "wrapStrategy": "WRAP"}
        ws.format(f"A{L_TITRE_MOD}:{DERNIERE}{L_TITRE_MOD}", bandeau)
        ws.format(f"A{L_TITRE_CLASS}:{DERNIERE}{L_TITRE_CLASS}", bandeau)
        ws.format(f"A{L_ENTETE_MOD}:{DERNIERE}{L_ENTETE_MOD}", entete)
        ws.format(f"A{L_ENTETE_CLASS}:{DERNIERE}{L_ENTETE_CLASS}", entete)
        ws.format(f"A{L_MODULE}:{DERNIERE}{L_MODULE + n_mod - 1}",
                  {"backgroundColor": FOND_MODULE})
        ws.format(f"A{L_METHODE}:{DERNIERE}{L_METHODE}", {
            "backgroundColor": FOND_METHODE, "wrapStrategy": "WRAP",
            "verticalAlignment": "TOP",
            "textFormat": {"italic": True, "fontSize": 9, "foregroundColor": GRIS}})
        ws.rows_auto_resize(L_METHODE - 1, L_METHODE)

        # le corps : blanc partout, puis fond d'ALERTE sur les non notes. Le fond
        # disparait tout seul des que la valeur ou la note est remplie.
        if corps:
            ws.format(f"A{L_CORPS}:{DERNIERE}{L_CORPS + len(corps) - 1}",
                      {"backgroundColor": BLANC})
            alertes = [L_CORPS + i for i, l in enumerate(corps)
                       if l[I["etat"]] == ALERTE]
            for debut, fin in _blocs(alertes):
                ws.format(f"A{debut}:{DERNIERE}{fin}",
                          {"backgroundColor": FOND_ALERTE})
        ws.freeze(rows=L_ENTETE_MOD)
    except Exception as e:
        print(f"habillage : {e}", flush=True)

    ecarts = [l[I["ecart"]] for l in corps if isinstance(l[I["ecart"]], int)]
    print(f"\n🏆A-CLASSEMENT : {len(module)} a noter + {len(corps)} au classement "
          f"en {time.time() - t0:.0f}s")
    print(f"  dont a venir : {sum(1 for l in module if l[I['etat']] == A_VENIR)}")
    print(f"  anciens en alerte (non notes) : "
          f"{sum(1 for l in corps if l[I['etat']] == ALERTE)}")
    print(f"  ecart avec la note suggeree : {sum(1 for e in ecarts if e)} "
          f"(dont {sum(1 for e in ecarts if abs(e) >= 2)} de 2 crans ou +)")
    return 0


def _blocs(lignes: List[int]):
    """Regroupe des numeros de ligne consecutifs en (debut, fin) — une requete de
    format par bloc au lieu d'une par ligne."""
    for i, n in enumerate(sorted(lignes)):
        if i == 0:
            debut = prec = n
            continue
        if n != prec + 1:
            yield debut, prec
            debut = n
        prec = n
    if lignes:
        yield debut, prec


if __name__ == "__main__":
    raise SystemExit(main())
