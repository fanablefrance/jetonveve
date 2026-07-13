"""
CHANTIER C — la page 🏆A-CLASSEMENT : une ligne par SERIE comic VeVe.

CE QUI EST AUTOMATIQUE (jamais a saisir, jamais ecrase par toi)
    serie, series_uuid, url, marque, licensor, date du drop, annee du volume,
    era (deduite), supply, prix en gems, cover exclusive VeVe, nb de raretes.

CE QUI EST A TOI (saisi a la main, JAMAIS ecrase par le script)
    valeur_irl_98 · fa_key · bonus_perso · note · commentaire
    -> relus dans la page a chaque run et reecrits tels quels. Le premier run les
       amorce depuis ton classement historique (via 🔗A-RACCORD).

CE QUI EST CALCULE (a partir des deux)
    valeur_par_edition = valeur_irl_98 / supply       (ce que chaque NFT "adosse")
    multiple_entree    = valeur_par_edition / gems    (1 gem ~ 1 $) -> la cherte du drop
    score              = valeur_irl_98 / supply^0.25
    note_suggeree      = bandes sur le score, + bonus_perso en CRANS

LA GRILLE (calibree sur les 405 lignes CERTAIN de ton classement, 13/07)
    exposant 0.25 : le supply compte, mais 4x moins que la valeur. Trouve par
    balayage — c'est TA pratique, pas une theorie : la valeur seule reproduit
    deja 73,3 % de tes notes, l'exposant 0.25 monte a 76,8 % (99 % a un cran
    pres). La cover exclusive, testee, DEGRADE le score : tu ne t'en sers pas.
    Les seuils arrondis "propres" font perdre 7 points -> on garde les seuils
    ajustes.

La note_suggeree ne remplace JAMAIS ta note : les deux colonnes coexistent et la
colonne `ecart` montre ou elles divergent. C'est la que se cachent tes coquilles
— et tes intuitions.

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, NOUVEAUX_JOURS (defaut 45)
"""

from __future__ import annotations

import datetime as _dt
import json
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

# --- LA GRILLE -------------------------------------------------------------
ECHELLE = ["C", "CC", "CCC", "B", "BB", "BBB", "A", "AA", "AAA"]
EXPOSANT_SUPPLY = 0.25
SEUILS = [1.0, 16.2, 34.2, 77.0, 220.4, 974.6, 7446.4, 33437.0]   # 8 coupures = 9 crans

MANUELLES = ["valeur_irl_98", "fa_key", "bonus_perso", "note", "commentaire"]

HEADER = [
    "etat", "veve_series_name", "date_drop", "era", "supply", "prix_gems",
    "valeur_irl_98", "note", "note_suggeree", "ecart",
    "valeur_par_edition", "multiple_entree", "score",
    "fa_key", "bonus_perso", "commentaire",
    "cover_exclusive", "nb_raretes", "veve_brand", "veve_licensor",
    "start_year", "veve_url", "series_uuid",
]
COL_ETAT, COL_VALEUR, COL_NOTE, COL_ECART, COL_SCORE = 0, 6, 7, 9, 12

A_VENIR = "🔴 A VENIR"
NOUVEAU = "🆕 NOUVEAU"

# Fonds de ligne (pastel, comme le reste du Sheet)
FOND_A_VENIR = {"red": 1.0, "green": 0.90, "blue": 0.80}    # orange clair
FOND_NOUVEAU = {"red": 1.0, "green": 0.98, "blue": 0.83}    # jaune clair

# L'URL VeVe d'un comic. /collection/comic/<id> renvoyait un 404 (bug corrige le
# 13/07 aussi dans veve_scraper.py). On la reconstruit ICI plutot que de recopier
# la colonne du catalogue : la page est juste des le 1er run, sans attendre que le
# daily ait reecrit les 16 000 lignes.
URL_COMIC = "https://www.veve.me/collectibles/en/comics/{uuid}"

# Origine des numeros de serie Google Sheets (jour 0 = 30/12/1899).
_EPOCH = _dt.datetime(1899, 12, 30)


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


def _num(x: Any) -> Optional[float]:
    if x in (None, ""):
        return None
    try:
        return float(str(x).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _date(x: Any) -> Optional[_dt.datetime]:
    """La date de drop, quel que soit ce que le Sheet nous renvoie.

    ⚠️ On lit le catalogue en UNFORMATTED (obligatoire pour les decimales en locale
    FR), et dans ce mode une cellule DATE revient en NUMERO DE SERIE Google
    (46 200,625) — pas en texte. La colonne date_drop affichait donc des nombres.
    On reconvertit ; les formats texte restent tolerees.
    """
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


def note_calculee(valeur: Optional[float], supply: Optional[float],
                  bonus: Optional[float]) -> str:
    """La note suggeree : bandes sur le score, decalees de `bonus` CRANS.

    `bonus_perso` est le seul jugement que la donnee ne peut pas rendre : la
    popularite du personnage chez les collectionneurs (Venom, Miles Morales...),
    que le prix du comic physique sous-estime. +1 = un cran au-dessus, -1 = un
    cran en dessous.
    """
    if not valeur or not supply or supply <= 0:
        return ""
    score = valeur / (supply ** EXPOSANT_SUPPLY)
    k = sum(1 for s in SEUILS if score > s)
    k = max(0, min(len(ECHELLE) - 1, k + int(bonus or 0)))
    return ECHELLE[k]


def _score(valeur: Optional[float], supply: Optional[float]) -> str:
    if not valeur or not supply or supply <= 0:
        return ""
    return round(valeur / (supply ** EXPOSANT_SUPPLY), 1)


def _client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON manquant.")
    return gspread.authorize(
        Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES))


def load_series(sh) -> Dict[str, Dict[str, Any]]:
    """Une entree par SERIE (les 5 lignes de rarete sont repliees en une)."""
    # UNFORMATTED obligatoire (locale FR : "6,99" numerise donnerait 699).
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
                "prix_gems": _num(r.get("store_price_gems")),
                "cover_exclusive": r.get("veve_exclusive", ""),
                "veve_brand": r.get("veve_brand", ""),
                "veve_licensor": r.get("veve_licensor", ""),
                "start_year": int(annee) if annee else "",
                "veve_url": URL_COMIC.format(uuid=uid),
                "nb_raretes": 0,
            }
        s["nb_raretes"] += 1

    # --- controles (repondent a "manque-t-il des series ?") ---
    sans_supply = [s for s in out.values() if not s["supply"]]
    sans_prix = [s for s in out.values() if not s["prix_gems"]]
    sans_date = [s for s in out.values() if not s["date_drop"]]
    dernier = max((s["date_drop"] for s in out.values() if s["date_drop"]),
                  default=None)
    print(f"Catalogue : {len(out)} series comics.", flush=True)
    print(f"  sans supply : {len(sans_supply)} (fiches delistees chez VeVe)", flush=True)
    print(f"  sans prix   : {len(sans_prix)}", flush=True)
    print(f"  sans date   : {len(sans_date)}", flush=True)
    print(f"  drop le plus recent connu : "
          f"{dernier.strftime('%d/%m/%Y %H:%M') if dernier else '-'}", flush=True)
    return out


def load_manuel(sh, series: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Les colonnes MANUELLES : la page existante d'abord, le classement historique
    (via 🔗A-RACCORD) pour amorcer ce qui manque."""
    manuel: Dict[str, Dict[str, Any]] = {}

    # 1) l'amorce : le classement historique, une seule fois par serie
    try:
        for r in sh.worksheet(RACCORD_TAB).get_all_records(
                value_render_option="UNFORMATTED_VALUE"):
            statut = str(r.get("statut", "")).strip()
            valide = str(r.get("valide", "") or "").strip()
            uid = str(r.get("series_uuid", "") or "").strip()
            if valide and len(valide) > 10:      # Preda a colle un autre uuid
                uid = valide
            if not uid or uid not in series:
                continue
            if statut != "CERTAIN" and not valide:
                continue                          # non tranche -> on n'amorce pas
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

    # 2) LA PAGE EXISTANTE A TOUJOURS RAISON : ce que Preda a saisi ou corrige
    #    dans 🏆A-CLASSEMENT ecrase l'amorce. Sinon une relance rendrait a une
    #    ligne la valeur qu'il vient justement de corriger.
    try:
        repris = 0
        for r in sh.worksheet(CLASSEMENT_TAB).get_all_records(
                value_render_option="UNFORMATTED_VALUE"):
            uid = str(r.get("series_uuid", "") or "").strip()
            if not uid:
                continue
            saisi = {c: r.get(c, "") for c in MANUELLES}
            if any(str(v).strip() for v in saisi.values()):
                manuel[uid] = saisi
                repris += 1
        print(f"Saisies reprises de la page : {repris} series.", flush=True)
    except gspread.WorksheetNotFound:
        print("🏆A-CLASSEMENT : creation.", flush=True)

    return manuel


def _etat(d: Optional[_dt.datetime], jours: int) -> str:
    """A VENIR = le drop n'a pas encore eu lieu (c'est la que Preda doit noter AVANT
    de poster sa carte Discord). NOUVEAU = sorti dans la fenetre recente."""
    if d is None:
        return ""
    now = _dt.datetime.utcnow()
    if d > now:
        return A_VENIR
    if d >= now - _dt.timedelta(days=jours):
        return NOUVEAU
    return ""


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERROR: SHEET_ID requis.", file=sys.stderr)
        return 2
    jours = int(os.environ.get("NOUVEAUX_JOURS", "45"))

    sh = _client().open_by_key(sheet_id)
    series = load_series(sh)
    manuel = load_manuel(sh, series)

    # PERIMETRE : les series notees + tout drop recent ou a venir (il faut bien
    # que les nouveautes arrivent quelque part pour etre notees).
    garde = set(manuel)
    nouveaux = {uid for uid, s in series.items()
                if uid not in garde and _etat(s["date_drop"], jours)}
    garde |= nouveaux
    print(f"Perimetre : {len(garde)} series ({len(nouveaux)} nouveaux drops).",
          flush=True)

    lignes: List[List[Any]] = []
    for uid in garde:
        s = series[uid]
        m = manuel.get(uid, {c: "" for c in MANUELLES})
        valeur = _num(m.get("valeur_irl_98"))
        supply = s["supply"]
        gems = s["prix_gems"]
        bonus = _num(m.get("bonus_perso")) or 0
        d = s["date_drop"]

        par_edition = round(valeur / supply, 4) if (valeur and supply) else ""
        multiple = (round(par_edition / gems, 2)
                    if (par_edition != "" and gems) else "")
        suggeree = note_calculee(valeur, supply, bonus)
        note = str(m.get("note", "") or "").strip()
        ecart = ""
        if note and suggeree and note in ECHELLE:
            ecart = ECHELLE.index(note) - ECHELLE.index(suggeree)

        lignes.append([
            _etat(d, jours), s["veve_series_name"],
            d.strftime("%d/%m/%Y %H:%M") if d else "",
            _era(s["start_year"] or None),
            supply if supply else "", gems if gems else "",
            m.get("valeur_irl_98", ""), note, suggeree, ecart,
            par_edition, multiple, _score(valeur, supply),
            m.get("fa_key", ""), m.get("bonus_perso", ""), m.get("commentaire", ""),
            s["cover_exclusive"], s["nb_raretes"], s["veve_brand"],
            s["veve_licensor"], s["start_year"], s["veve_url"], uid,
        ])

    # TRI : ce qui demande une action d'abord.
    #   1. les drops A VENIR (a noter AVANT la carte Discord), le plus proche en tete
    #   2. les NOUVEAUX pas encore notes
    #   3. le reste, par score decroissant (le classement proprement dit)
    def cle(l):
        etat, note = l[COL_ETAT], l[COL_VALEUR]
        sc = l[COL_SCORE] if isinstance(l[COL_SCORE], (int, float)) else -1
        if etat == A_VENIR:
            return (0, l[2], 0)          # le drop le plus imminent en premier
        if etat == NOUVEAU and not note:
            return (1, "", 0)
        return (2, "", -sc)
    lignes.sort(key=cle)

    try:
        ws = sh.worksheet(CLASSEMENT_TAB)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=CLASSEMENT_TAB, rows=len(lignes) + 50,
                              cols=len(HEADER))
    ws.update(range_name="A1", values=[HEADER] + lignes, value_input_option="RAW")

    # --- habillage : les drops a venir doivent SAUTER AUX YEUX ---
    try:
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
        derniere = chr(ord("A") + len(HEADER) - 1)
        # les lignes sont triees : les blocs sont contigus, une requete par bloc
        for etat, fond in ((A_VENIR, FOND_A_VENIR), (NOUVEAU, FOND_NOUVEAU)):
            idx = [i + 2 for i, l in enumerate(lignes) if l[COL_ETAT] == etat]
            if not idx:
                continue
            ws.format(f"A{min(idx)}:{derniere}{max(idx)}",
                      {"backgroundColor": fond})
        # remise a blanc du reste (sinon les couleurs d'hier restent sur des lignes
        # qui ne sont plus des nouveautes)
        reste = [i + 2 for i, l in enumerate(lignes) if not l[COL_ETAT]]
        if reste:
            ws.format(f"A{min(reste)}:{derniere}{len(lignes) + 1}",
                      {"backgroundColor": {"red": 1, "green": 1, "blue": 1}})
    except Exception as e:
        print(f"format warning: {e}", flush=True)

    a_venir = sum(1 for l in lignes if l[COL_ETAT] == A_VENIR)
    notees = sum(1 for l in lignes if l[COL_NOTE])
    desaccords = sum(1 for l in lignes
                     if isinstance(l[COL_ECART], int) and abs(l[COL_ECART]) >= 1)
    gros = sum(1 for l in lignes
               if isinstance(l[COL_ECART], int) and abs(l[COL_ECART]) >= 2)
    print(f"\n🏆A-CLASSEMENT : {len(lignes)} series en {time.time() - t0:.0f}s")
    print(f"  🔴 A VENIR (en haut, fond orange) : {a_venir}")
    print(f"  notees a la main : {notees}")
    print(f"  ecart avec la note suggeree : {desaccords} (dont {gros} de 2 crans ou +)")
    print(f"  a noter (valeur IRL vide) : {sum(1 for l in lignes if not l[COL_VALEUR])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
