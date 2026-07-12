"""SONDE : peut-on supprimer le pass GraphQL « editions » du daily ? (12/07)

CONTEXTE (audit du Sheet). Chaque nuit, le daily appelle VeVe GraphQL pour
~2 600 collectibles (`dynamic_run` avec DYNAMIC_WITH_EDITIONS=true) afin de
rafraichir : veve_store_price, veve_total_available, sold_editions,
editions_in_circulation, burned_editions, withheld_editions, store_allocation.

DEUX PREUVES DEJA FAITES EN SESSION (base DuckDB + _DynState) :
  * `veve_total_available` = releaseAmount − mints on-chain, EXACTEMENT
    (8a50b96b : 1099 − 600 = 499 ✓ · 2c3b5185 : 777 − 558 = 219 ✓) ;
  * `editions_in_circulation` = releaseAmount − withheld_editions sur 8/8 items
    -> ce n'est PAS une donnee dynamique, c'est une constante.
Et le tracker my-nft-tracker (deja appele chaque nuit pour le floor, donc
GRATUIT) expose `storePrice`, `availableAmount` et `releaseAmount`.

CE QUE LA SONDE VERIFIE (un seul run, puis on decide) : le tracker dit-il la
MEME chose que GraphQL, item par item ?
    storePrice        (tracker)  ==  veve_store_price        (GraphQL) ?
    availableAmount   (tracker)  ==  veve_total_available    (GraphQL) ?
    releaseAmount     (tracker)  ==  releaseAmount           (GraphQL) ?
    releaseAmount − withheld     ==  editions_in_circulation (GraphQL) ?

VERDICT :
  * si le tracker colle -> on SUPPRIME les ~2 600 appels GraphQL par nuit
    (les 2 champs restants, withheld_editions et store_allocation, sont
    STATIQUES : ils partent dans le catalogue froid, rafraichi seulement sur
    les nouveautes) ;
  * s'il y a des ecarts -> la sonde les liste (jusqu'a 15) pour comprendre AVANT
    de toucher au pipeline. Aucun onglet n'est modifie : la sonde ne fait que
    lire et afficher.

Env : SHEET_ID (optionnel), PROBE_MAX (0 = tous les collectibles).
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from typing import Dict

from scraper.veve_scraper import scrape_catalogue
from scraper import veve_detail

PROBE_MAX = int(os.environ.get("PROBE_MAX", "0") or "0")


def _n(x):
    """Nombre comparable (le Sheet et les APIs melangent str/float/None)."""
    if x in (None, ""):
        return None
    try:
        return round(float(str(x).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def main() -> int:
    t0 = time.time()

    print("1) Tracker (pages deja payees chaque nuit pour le floor)...",
          flush=True)
    colls = [p for p in scrape_catalogue(category="collectible",
                                         limit_total=PROBE_MAX or None)
             if p.get("veve_uuid")]
    if not colls:
        print("Tracker vide — sonde interrompue.", file=sys.stderr)
        return 1
    trk: Dict[str, Dict] = {p["veve_uuid"]: p for p in colls}
    print(f"   {len(trk)} collectibles.", flush=True)

    print("2) VeVe GraphQL (le pass qu'on veut supprimer) — derniere fois...",
          flush=True)
    gql = veve_detail.enrich_dynamic(list(trk), is_comic=False)
    print(f"   {len(gql)} reponses.", flush=True)

    verdict = Counter()
    ecarts = {"prix": [], "dispo": [], "release": [], "circulation": []}

    for uid, g in gql.items():
        t = trk.get(uid) or {}
        checks = (
            ("prix", _n(t.get("storePrice")), _n(g.get("veve_store_price"))),
            ("dispo", _n(t.get("availableAmount")),
             _n(g.get("veve_total_available"))),
            ("release", _n(t.get("releaseAmount")),
             _n(g.get("rarity_editions"))),
        )
        for nom, a, b in checks:
            if a is None or b is None:
                verdict[f"{nom}:vide"] += 1
            elif a == b:
                verdict[f"{nom}:OK"] += 1
            else:
                verdict[f"{nom}:ECART"] += 1
                if len(ecarts[nom]) < 15:
                    ecarts[nom].append((t.get("name") or uid[:8], a, b))
        # editions_in_circulation = releaseAmount − withheld ?
        rel = _n(t.get("releaseAmount"))
        wh = _n(g.get("withheld_editions"))
        circ = _n(g.get("editions_in_circulation"))
        if rel is None or wh is None or circ is None:
            verdict["circulation:vide"] += 1
        elif rel - wh == circ:
            verdict["circulation:OK"] += 1
        else:
            verdict["circulation:ECART"] += 1
            if len(ecarts["circulation"]) < 15:
                ecarts["circulation"].append(
                    (t.get("name") or uid[:8], rel - wh, circ))

    print("\n===== VERDICT (tracker vs GraphQL) =====", flush=True)
    for nom in ("prix", "dispo", "release", "circulation"):
        ok = verdict[f"{nom}:OK"]
        ko = verdict[f"{nom}:ECART"]
        vide = verdict[f"{nom}:vide"]
        tot = ok + ko or 1
        print(f"  {nom:12s} identiques {ok:5d}  ecarts {ko:5d}  "
              f"({100.0 * ok / tot:5.1f} % OK)  vides {vide}", flush=True)

    for nom, lst in ecarts.items():
        if lst:
            print(f"\n  exemples d'ecarts — {nom} (tracker | GraphQL) :",
                  flush=True)
            for name, a, b in lst:
                print(f"    {str(name)[:45]:45s} {a} | {b}", flush=True)

    print(f"\nSonde terminee en {time.time() - t0:.0f}s. "
          f"Aucun onglet modifie.", flush=True)
    print("Si 'prix' et 'dispo' sont ~100 % OK : le pass GraphQL nocturne "
          "peut sauter (~2 600 requetes/nuit en moins).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN editions_probe.py
