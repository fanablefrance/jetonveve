"""🐋 EXPORT DES COMPTES SUIVIS — le pont vers jetonveve (canal whale/team).

Le tag « Type de compte » (VeVe Team / Fondateur / Moderation / Publisher /
Influenceur / A suivre) vit dans l'onglet 🟣C-PSEUDOS du Sheet, donc chez preda
(PRIVE). Le module `whale_watch.py` (jetonveve, PUBLIC) ne peut pas lire le
Sheet : on lui EXPORTE les seules lignes taguees dans `data/tracked_accounts.csv`
qu'il lit en sparse-checkout. Meme mecanique que `export_elements.py`.

On n'exporte QUE les lignes AVEC un tag (une ligne sans « Type de compte » n'est
pas suivie) et on n'ecrase JAMAIS le CSV avec du vide si la lecture echoue (la
recolte est sacree). Colonnes : username, type, wallet_imx, wallet_stackr,
holdings, value_floor (les deux derniers enrichissent la carte Discord).

Env : GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, TRACKED_CSV.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from typing import List

from scraper.sheets import _client, _open_worksheet, append_log
from scraper.stackr import PSEUDOS_TAB, PSEUDOS_HEADER

CSV_PATH = os.environ.get("TRACKED_CSV", "data/tracked_accounts.csv")
ENTETE = ["username", "type", "wallet_imx", "wallet_stackr",
          "holdings", "value_floor"]
TYPE_COL = "Type de compte"


def _retry(quoi, fn, essais=5):
    for i in range(1, essais + 1):
        try:
            return fn()
        except Exception as e:                                  # noqa: BLE001
            if i == essais:
                raise
            print(f"  {quoi} : {e} — nouvel essai ({i}/{essais})",
                  file=sys.stderr)
            time.sleep(min(60, 3 * 2 ** i))


def main() -> int:
    sid = os.environ.get("SHEET_ID")
    if not sid:
        print("SHEET_ID manquant.", file=sys.stderr)
        return 2
    sh = _retry("ouverture du Sheet", lambda: _client().open_by_key(sid))
    ws = _retry("ouverture 🟣C-PSEUDOS",
                lambda: _open_worksheet(sh, PSEUDOS_TAB, cols=len(PSEUDOS_HEADER)))
    rows = _retry("lecture 🟣C-PSEUDOS",
                  lambda: ws.get_all_records() if ws.row_count > 1 else [])
    if rows is None:
        print("⛔ 🟣C-PSEUDOS illisible — on ne touche pas au CSV existant.",
              file=sys.stderr)
        return 3

    out: List[List] = []
    for r in rows:
        typ = str(r.get(TYPE_COL, "")).strip()
        if not typ:
            continue                          # seul le tag fait suivre
        out.append([str(r.get("username", "")).strip(), typ,
                    str(r.get("wallet_imx", "")).strip(),
                    str(r.get("wallet_stackr", "")).strip(),
                    str(r.get("holdings", "")).strip(),
                    str(r.get("value_floor", "")).strip()])

    os.makedirs(os.path.dirname(CSV_PATH) or ".", exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(ENTETE)
        w.writerows(out)

    par_type = {}
    for r in out:
        par_type[r[1]] = par_type.get(r[1], 0) + 1
    detail = " · ".join(f"{k}={v}" for k, v in sorted(par_type.items())) or "aucun"
    print(f"🐋 {len(out)} compte(s) suivi(s) exporte(s) ({detail}) -> {CSV_PATH}",
          flush=True)
    try:
        append_log(sid, "export_tracked", "OK", f"{len(out)} comptes · {detail}")
    except Exception:                                           # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
