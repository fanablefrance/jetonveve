# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/burns_fraicheur.py   (NEUF)
# ═══════════════════════════════════════════════════════════════════════════════
# 🔎 LE BURN DU JOUR EST-IL LA ? — une mesure, pas une supposition
# ═══════════════════════════════════════════════════════════════════════════════
#
# 🔴 POURQUOI CE FICHIER EXISTE
# ─────────────────────────────────────────────────────────────────────────────
# `filet.yml` affirmait, a chaque echec de `burns.yml` :
#     « Le burn du jour manque : le post BURN de Discord gardera celui de la
#       veille. »
# C'etait une SUPPOSITION tiree de la couleur du run. Elle devient FAUSSE des
# qu'un rattrapage existe : un run peut echouer a 02 h et le jour etre la a
# 10 h. ⇒ On mesure le FICHIER, plus jamais la couleur du run.
#
# 🔬 CE QUI A ETE MESURE (21/08/2026, sur les vrais commits du depot)
# ─────────────────────────────────────────────────────────────────────────────
#   Apres CHAQUE run vert, la derniere date de `data/burns_daily.csv` vaut la
#   veille de la date du run — ou le jour meme quand le run est tardif :
#     commit 18/08 01:58 -> fin 2026-08-17     (veille)
#     commit 15/08 01:55 -> fin 2026-08-14     (veille)
#     commit 14/08 03:05 -> fin 2026-08-13     (veille)
#     commit 11/08 02:33 -> fin 2026-08-10     (veille)
#     commit 06/08 10:17 -> fin 2026-08-06     (jour meme, run tardif)
#   Et au 21/08, apres 3 nuits rouges : fin 2026-08-19 — soit J-2. ⇒ MANQUE.
#
# ⇒ 🔑 LA REGLE, ECRITE POUR NE JAMAIS CRIER SUR UN ETAT NORMAL :
#       le jour MANQUE quand   derniere_date  <  aujourd'hui (UTC) - 1 jour
#   Le « jour meme » et « la veille » passent tous les deux. Seul J-2 et
#   au-dela declenchent.
#
# ⚠️ CE QUE CE FICHIER NE MESURE PAS : il lit le CSV du depot, pas le Sheet ni
#   le post Discord. Si un jour le post BURN cessait de lire ce CSV, cette
#   mesure ne dirait plus rien de ce que Preda voit. C'est le CSV qui fait foi
#   aujourd'hui (burns.py, `write_sheet` : « le CSV fait foi »).

from __future__ import annotations

import csv
import datetime as _dt
import os
import sys

DAILY_CSV = os.path.join("data", "burns_daily.csv")


def derniere_date(chemin: str = DAILY_CSV) -> str | None:
    """La date la plus recente de burns_daily.csv, ou None si illisible/vide.

    ⭐ On prend le MAXIMUM, pas la derniere ligne : le fichier est trie
    aujourd'hui, mais une mesure qui repose sur un tri qu'elle ne verifie pas
    ment le jour ou le tri change.
    """
    try:
        with open(chemin, newline="", encoding="utf-8") as f:
            lignes = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return None
    dates = []
    for r in lignes:
        d = (r.get("date") or "").strip()
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            dates.append(d)
    return max(dates) if dates else None


def seuil(aujourdhui: _dt.date) -> str:
    """La date la plus ancienne encore acceptable : la veille."""
    return (aujourdhui - _dt.timedelta(days=1)).isoformat()


def manque(chemin: str = DAILY_CSV,
           aujourdhui: _dt.date | None = None) -> bool:
    """True quand le burn du jour manque encore.

    ⛔ Un fichier illisible ou vide compte comme MANQUANT : se taire sur une
    donnee absente est exactement la panne qu'on cherche a voir.
    """
    if aujourdhui is None:
        aujourdhui = _dt.datetime.now(_dt.timezone.utc).date()
    d = derniere_date(chemin)
    if d is None:
        return True
    return d < seuil(aujourdhui)


def main() -> int:
    chemin = sys.argv[1] if len(sys.argv) > 1 else DAILY_CSV
    aujourdhui = _dt.datetime.now(_dt.timezone.utc).date()
    d = derniere_date(chemin)
    m = manque(chemin, aujourdhui)

    print(f"aujourd'hui (UTC) : {aujourdhui.isoformat()}")
    print(f"seuil accepte     : >= {seuil(aujourdhui)}")
    print(f"derniere date     : {d or '(aucune — fichier illisible ou vide)'}")
    print(f"verdict           : {'🔴 LE JOUR MANQUE' if m else '✅ le jour est la'}")

    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write(f"manque={'oui' if m else 'non'}\n")
            f.write(f"derniere={d or ''}\n")
            f.write(f"seuil={seuil(aujourdhui)}\n")
    # ⭐⭐ Sortie 0 dans les DEUX cas : ce module CONSTATE, il ne juge pas.
    # C'est le workflow qui decide quoi faire du verdict — sortir en echec ici
    # rendrait impossible de lire le verdict sans faire rougir l'etape.
    return 0


if __name__ == "__main__":
    sys.exit(main())
