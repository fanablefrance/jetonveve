"""🔀 FUSION DE L'ETAT — parce qu'un conflit git ne doit JAMAIS jeter la recolte.

Le 14/07, le run s'est termine par :
    CONFLICT (content): Merge conflict in data/floor_state.json
    error: could not apply ... floor_watch: etat
Deux runs ont ecrit l'etat, le rebase a bute, et **l'etat du run n'a pas ete
pousse** : ses ventes decouvertes, ses floors StackR memorises, son budget de
messages — tout perdu. C'est exactement la regle des collecteurs longs : LA
RECOLTE EST SACREE. Un `git pull --rebase` qui echoue n'est pas un incident
technique, c'est une perte de donnees.

⚠️ Et surtout : NE PAS ecraser betement (« la mienne gagne »). Les deux etats
sont VRAIS — chacun a vu des choses que l'autre n'a pas vues. On FUSIONNE :
  * un dictionnaire : l'union des cles ;
  * une collision : c'est l'observation la PLUS RECENTE qui l'emporte ;
  * le budget de messages : le PLUS GRAND compteur du jour (sinon on
    re-autoriserait des messages deja envoyes — un plafond qui se remet a zero
    tout seul n'est pas un plafond).

Usage : python -m scraper.floor_state_merge <mien.json> <celui_du_depot.json>
        -> ecrit la fusion dans <celui_du_depot.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict


def _ts(v: Any) -> float:
    """L'horodatage d'une valeur d'etat, quelle que soit sa forme."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list) and len(v) > 1 and isinstance(v[1], (int, float)):
        return float(v[1])          # sales : [prix_usd, ts, timestamp]
    return 0.0


def fusionner(mien: Dict, autre: Dict) -> Dict:
    out: Dict[str, Any] = dict(autre)
    for cle, v in (mien or {}).items():
        a = out.get(cle)
        if cle == "budget":
            # Le plafond du jour : on garde le PLUS GRAND compteur. Deux runs
            # qui ont envoye 3 messages chacun en ont envoye 3 au minimum — on
            # ne re-autorise pas ce qui est deja parti.
            if isinstance(v, dict) and isinstance(a, dict) \
                    and a.get("jour") == v.get("jour"):
                out[cle] = {"jour": v.get("jour"),
                            "n": max(int(a.get("n") or 0), int(v.get("n") or 0))}
            else:
                out[cle] = v
        elif isinstance(v, dict) and isinstance(a, dict):
            f = dict(a)
            for k, val in v.items():
                if k not in f or _ts(val) >= _ts(f[k]):
                    f[k] = val      # la plus RECENTE des deux observations
            out[cle] = f
        else:
            out[cle] = v
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: floor_state_merge <mien.json> <depot.json>",
              file=sys.stderr)
        return 2
    mien_p, depot_p = sys.argv[1], sys.argv[2]

    def _lire(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                   # noqa: BLE001
            return {}

    mien, depot = _lire(mien_p), _lire(depot_p)
    out = fusionner(mien, depot)
    with open(depot_p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"🔀 etat fusionne : "
          + " · ".join(f"{k}={len(v)}" for k, v in out.items()
                       if isinstance(v, dict)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
