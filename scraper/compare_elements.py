# ⚠️ DEPOT : VeVePreda/scrapeur-veve   ·   CHEMIN : scraper/compare_elements.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🔬 LE COMPARATEUR v1/v2 du pont elements.csv — la phase « double en silence ».

Il ne decide RIEN : il mesure, colonne par colonne, ce qui separerait le pont
v2 (tracker) du pont v1 (onglets froids) si on basculait aujourd'hui.

Deux familles de colonnes, deux verdicts :
  * STABLES (identite, supply, first_public, note) : tout ecart est un
    PROBLEME a comprendre avant bascule — elles ne bougent pas d'heure en
    heure, une difference n'a pas d'excuse temporelle.
  * VIVANTES (listings, atl/ath + dates) : v1 date du dernier daily, v2 du
    scrape de l'instant — un ecart PEUT n'etre que du temps qui passe. On
    donne le taux et des exemples ; c'est la TENDANCE sur plusieurs jours
    qui compte (un taux qui ne baisse pas apres la reparation ×100 serait
    un signal).

Sortie : rapport dans le log, code retour 0 toujours (l'observation ne casse
pas un run). `COMPARE_SEUIL_STABLE` (defaut 0) : au-dela de N ecarts sur les
colonnes stables, code retour 1 pour faire remarquer le run.
"""

from __future__ import annotations

import csv
import os
import sys
from typing import Dict, List

V1 = os.environ.get("ELEMENTS_CSV", "data/elements.csv")
V2 = os.environ.get("ELEMENTS_V2", "data/elements_v2.csv")
SEUIL = int(os.environ.get("COMPARE_SEUIL_STABLE", "0"))

STABLES = ["series_uuid", "name", "category", "rarity", "edition_type",
           "supply", "first_public", "note", "brand", "licensor"]
VIVANTES = ["listings", "atl", "atl_date", "ath", "ath_date"]


def _lire(chemin: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    with open(chemin, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            uid = (r.get("veve_uuid") or "").strip()
            if uid:
                out[uid] = r
    return out


def _egal(col: str, a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return True
    # nombres : 6.99 == 6,99 == "6.990" ; tolerance 1 centime sur les prix
    try:
        fa = float(a.replace(",", ".").replace(" ", ""))
        fb = float(b.replace(",", ".").replace(" ", ""))
        return abs(fa - fb) <= (0.01 if col in ("atl", "ath") else 0)
    except (TypeError, ValueError):
        return False


def main() -> int:
    for chemin in (V1, V2):
        if not os.path.exists(chemin):
            print(f"⛔ {chemin} absent — rien a comparer.", file=sys.stderr)
            return 0
    v1, v2 = _lire(V1), _lire(V2)
    seulement_v1 = sorted(set(v1) - set(v2))
    seulement_v2 = sorted(set(v2) - set(v1))
    communs = sorted(set(v1) & set(v2))

    print("══════════════════════════════════════════════════════")
    print(f"  COMPARATEUR PONT v1 (onglets) / v2 (tracker)")
    print(f"  v1 : {len(v1)} lignes · v2 : {len(v2)} lignes · "
          f"communes : {len(communs)}")
    if seulement_v1:
        print(f"  ⚠️ {len(seulement_v1)} uuid SEULEMENT en v1 — ex : "
              + ", ".join(u[:8] for u in seulement_v1[:5]))
    if seulement_v2:
        print(f"  ℹ️ {len(seulement_v2)} uuid SEULEMENT en v2 — ex : "
              + ", ".join(u[:8] for u in seulement_v2[:5]))

    total_stable = 0
    for fam, cols in (("STABLES (0 ecart attendu)", STABLES),
                      ("VIVANTES (drift temporel tolere)", VIVANTES)):
        print(f"  ── {fam} " + "─" * (40 - len(fam)))
        for col in cols:
            diffs: List[str] = [u for u in communs
                                if not _egal(col, v1[u].get(col, ""),
                                             v2[u].get(col, ""))]
            if col in STABLES:
                total_stable += len(diffs)
            pct = 100.0 * len(diffs) / max(len(communs), 1)
            marque = " " if not diffs else ("⚠️" if col in STABLES else "≈")
            print(f"  {marque} {col:<14} {len(diffs):>6} ecart(s) ({pct:.2f} %)")
            for u in diffs[:3]:
                print(f"       {u[:8]}…  v1={v1[u].get(col, '')!r}  "
                      f"v2={v2[u].get(col, '')!r}")
    print("══════════════════════════════════════════════════════")
    if SEUIL and total_stable > SEUIL:
        print(f"⛔ {total_stable} ecart(s) sur les colonnes STABLES "
              f"(seuil {SEUIL}) — a comprendre avant toute bascule.",
              file=sys.stderr)
        return 1
    print(f"Verdict : {total_stable} ecart(s) stables au total. La bascule ne "
          f"se decide que sur plusieurs rapports consecutifs propres.",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
