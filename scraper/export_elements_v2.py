# ⚠️ DEPOT : VeVePreda/scrapeur-veve   ·   CHEMIN : scraper/export_elements_v2.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🌉 LE PONT v2 — elements.csv fabrique depuis le TRACKER, plus depuis les
onglets froids (chantier « le Sheet = interface, l'entrepot = magasin »).

POURQUOI (audit du 22/07/2026)
------------------------------
Le pont v1 exporte les onglets 🔵C-COLLECTIBLE / 🟢C-COMICS : le dernier
endroit ou des NOMBRES relus par du code ont une CELLULE pour source de
verite. C'est la famille de pannes des decimales FR ×100 (3 177 paires
reparees), des 429 et des 503. La v2 inverse la source :

  * identite, rarete, marque, licence, atl/ath (+dates), OFFRES EN VENTE
        -> le TRACKER, en direct (`scrape_catalogue`) — nombres canoniques,
           zero cellule sur le chemin. Bonus : `listings` devient
           market_totalListings du jour meme (v1 le prenait dans _DynState,
           rafraichi la veille).
  * note de classement -> le Sheet 🏆A-CLASSEMENT, SEULE lecture Sheet
        restante avec les deux colonnes ci-dessous : c'est un jugement
        manuel de Preda — le Sheet est la bonne source pour un geste humain.
  * supply + first_available_edition -> viennent de l'enrichissement GraphQL,
        qui n'existe QUE dans les onglets froids aujourd'hui. La v2 les lit
        encore la (2 colonnes ENTIERES — le ×100 ne mord que les decimales),
        c'est l'etape suivante du chantier (cache fichier) qui les detachera.

⭐ EN PRIME, LA PREUVE DEMANDEE PAR LE PLAN : le run MESURE l'accord entre
`releaseAmount` (tracker) et `supply` (GraphQL/onglets) — c'est cette mesure
qui dira si on peut un jour se passer completement des onglets pour le supply.

MODE « DOUBLE EN SILENCE »
--------------------------
Ce module ecrit `data/elements_v2.csv` et NE TOUCHE PAS a data/elements.csv :
la prod (jetonveve, signaux 🎯/📚) continue de lire la v1. On compare N jours
(scraper.compare_elements), et on ne bascule que sur un rapport propre.

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, ELEMENTS_V2 (defaut
      data/elements_v2.csv), ELEMENTS_SUPPLY_MAX (0 = tout, comme v1).
"""

from __future__ import annotations

import csv
import os
import sys
from typing import Any, Dict, List

# Meme en-tete que la v1, OCTET POUR OCTET : jetonveve ne verra aucune
# difference le jour de la bascule.
ENTETE = ["veve_uuid", "series_uuid", "name", "category", "rarity",
          "edition_type", "supply", "first_public", "listings", "note",
          "brand", "licensor", "atl", "atl_date", "ath", "ath_date"]

CSV_V2 = os.environ.get("ELEMENTS_V2", "data/elements_v2.csv")
SUPPLY_MAX = int(os.environ.get("ELEMENTS_SUPPLY_MAX", "0"))
CAP = 1e12                       # meme plafond d'aberration que veve_scraper


def _num(x) -> int:
    try:
        return int(float(str(x).replace(",", ".").replace(" ", "")
                         .replace(" ", "") or 0))
    except (TypeError, ValueError):
        return 0


def _prix(v) -> Any:
    """Prix canonique : nombre a point decimal, '' si absent/aberrant."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if not 0 < f < CAP:
        return ""
    return int(f) if f == int(f) else round(f, 2)


def _cat(p: Dict) -> str:
    return "comic" if str(p.get("category") or "").strip().lower() == "comic" \
        else "collectible"


def lire_enrichissement(sh) -> Dict[str, Dict[str, int]]:
    """{uuid -> {supply, first_public}} depuis les onglets froids.

    SEULES deux colonnes ENTIERES sont lues (le ×100 ne mordait que les
    decimales) — et c'est la partie que l'etape suivante du chantier
    remplacera par un cache fichier maintenu par le daily."""
    from scraper.sheets import COLLECT_TAB, COMICS_TAB
    from scraper.export_elements import _lire
    out: Dict[str, Dict[str, int]] = {}
    for tab in (COMICS_TAB, COLLECT_TAB):
        for r in _lire(sh.worksheet(tab), "veve_uuid"):
            uid = str(r.get("veve_uuid") or "").strip()
            if not uid:
                continue
            out[uid] = {
                "supply": _num(r.get("supply")),
                "first_public": _num(r.get("first_available_edition")),
                "series_uuid": str(r.get("series_uuid") or "").strip(),
            }
    return out


def construire_v2(produits: List[Dict], enrich: Dict[str, Dict],
                  notes: Dict[str, str]) -> List[List]:
    """Les lignes du pont, tracker d'abord.

    Regles REPRISES de la v1, a l'identique :
      * nom d'un COMIC = la SERIE ; d'un COLLECTIBLE = l'ITEM (corrige 17/07) ;
      * supply d'un comic = MAX par serie, jamais la somme (Captain America #7) ;
      * VIDE = INCONNU, jamais zero (listings, first_public) ;
      * note cherchee par serie PUIS par uuid.
    """
    # tirage des comics = MAX par serie, calcule sur l'enrichissement
    par_serie: Dict[str, int] = {}
    for uid, e in enrich.items():
        s = e.get("series_uuid") or ""
        if s and e["supply"]:
            par_serie[s] = max(par_serie.get(s, 0), e["supply"])

    out: List[List] = []
    stats = {"tracker": 0, "sans_enrich": 0, "hors_tracker": 0,
             "release_eq": 0, "release_diff": 0, "release_absent": 0}
    vus = set()

    for p in produits:
        uid = str(p.get("veve_uuid") or "").strip()
        if not uid:
            continue
        vus.add(uid)
        cat = _cat(p)
        e = enrich.get(uid) or {}
        if not e:
            stats["sans_enrich"] += 1
        serie = str(p.get("series_uuid") or "").strip() or e.get("series_uuid", "")
        supply = par_serie.get(serie, 0) if cat == "comic" \
            else int(e.get("supply") or 0)
        if SUPPLY_MAX and supply > SUPPLY_MAX:
            continue
        # ⭐ LA MESURE DU PLAN : releaseAmount (tracker) vs supply (GraphQL).
        ra = _num(p.get("releaseAmount"))
        if not ra:
            stats["release_absent"] += 1
        elif cat == "collectible" and e.get("supply"):
            stats["release_eq" if ra == e["supply"] else "release_diff"] += 1
        if cat == "comic":
            nom = (str(p.get("series_name") or "") or str(p.get("name") or "")).strip()
        else:
            nom = (str(p.get("name") or "") or str(p.get("series_name") or "")).strip()
        note = notes.get(serie) or notes.get(uid) or ""
        lst = p.get("market_totalListings")
        out.append([
            uid, serie, nom, cat,
            str(p.get("rarity") or "").strip(),
            str(p.get("edition") or "").strip(),
            supply,
            int(e["first_public"]) if e.get("first_public") else "",
            _num(lst) if lst not in (None, "") else "",   # vide = inconnu
            note,
            str(p.get("brand_name") or "").strip(),
            str(p.get("licensor_name") or "").strip(),
            _prix(p.get("atl")),
            str(p.get("atl_date") or "").strip(),
            _prix(p.get("ath")),
            str(p.get("ath_date") or "").strip(),
        ])
        stats["tracker"] += 1

    # Les items connus des onglets mais ABSENTS du tracker : on ne les perd
    # pas (le tracker peut retirer une fiche), mais sans prix — vide = inconnu.
    for uid, e in enrich.items():
        if uid in vus:
            continue
        stats["hors_tracker"] += 1
        # sans identite tracker on n'a ni nom ni rarete fiables : ligne minimale
        serie = e.get("series_uuid", "")
        supply = e.get("supply") or ""
        out.append([uid, serie, "", "", "", "", supply,
                    e.get("first_public") or "", "", notes.get(serie) or
                    notes.get(uid) or "", "", "", "", "", "", ""])

    out.sort(key=lambda l: (l[3], l[6] if l[6] != "" else 0, l[2]))

    eqb = stats["release_eq"] + stats["release_diff"]
    print(f"  v2 : {stats['tracker']} lignes tracker · "
          f"{stats['sans_enrich']} sans enrichissement (supply/1re ed. vides) · "
          f"{stats['hors_tracker']} connues des onglets mais hors tracker.",
          flush=True)
    if eqb:
        pct = 100.0 * stats["release_eq"] / eqb
        print(f"  ⭐ preuve releaseAmount vs supply (collectibles) : "
              f"{stats['release_eq']}/{eqb} identiques ({pct:.1f} %) · "
              f"{stats['release_absent']} sans releaseAmount. "
              f"{'-> a ' + format(pct, '.0f') + ' %, le cache fichier pourra un jour remplacer les onglets.' if pct >= 99 else '-> PAS assez concordant pour se passer des onglets : garder la source GraphQL.'}",
              flush=True)
    return out


def main() -> int:
    sid = os.environ.get("SHEET_ID")
    if not sid:
        print("SHEET_ID manquant.", file=sys.stderr)
        return 2
    from scraper.veve_scraper import scrape_catalogue
    from scraper.export_elements import _retry, lire_notes
    from scraper.sheets import _client

    produits = scrape_catalogue()
    if len(produits) < 10000:
        # Le catalogue fait ~19 000 produits : une recolte a moitie vide ne
        # doit jamais ecraser un pont — meme en silence, on mesure du vrai.
        print(f"⛔ recolte tracker trop maigre ({len(produits)}) — on n'ecrit "
              f"rien.", file=sys.stderr)
        return 3
    sh = _retry("ouverture du Sheet", lambda: _client().open_by_key(sid))
    enrich = _retry("lecture enrichissement (2 colonnes entieres)",
                    lambda: lire_enrichissement(sh))
    try:
        notes = _retry("lecture 🏆A-CLASSEMENT", lambda: lire_notes(sh))
    except Exception as e:                                  # noqa: BLE001
        print(f"  notes illisibles ({e}) — v2 sans note ce tour.",
              file=sys.stderr)
        notes = {}
    rows = construire_v2(produits, enrich, notes)
    if not rows:
        print("⛔ 0 ligne — rien d'ecrit.", file=sys.stderr)
        return 3
    os.makedirs(os.path.dirname(CSV_V2) or ".", exist_ok=True)
    with open(CSV_V2, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(ENTETE)
        w.writerows(rows)
    print(f"🌉 v2 : {len(rows)} elements -> {CSV_V2}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
