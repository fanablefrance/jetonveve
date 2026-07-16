"""🧬 merge_wallets — le VRAI first_seen de chaque wallet, sur les 3 eres.

But (demande Preda) : une source DEFINITIVE des dates de creation de wallet, la
plus juste possible. Le first_seen d'un wallet = le PLUS ANCIEN vu sur les trois
chaines VeVe successives — un OG cree sur GoChain en 2020 ne doit plus etre
ecrase a « genese IMX 2021 ».

Fusionne 3 registres wallets (tous des CSV PUBLICS committes, aucun token) :
  - CollectChain : wallet_registry_deep.csv (astronema) [wallet,first_seen,last_active,tx_count]
  - IMX          : wallet_registry_imx.csv  (paolo)     [wallet,first_seen,last_active,tx_count]
  - GoChain      : gochain_wallets.csv       (jetonveve) [wallet,first_seen,last_seen,tx,...]

Par wallet : first_seen = MIN des trois (date de creation reelle) · last_active =
MAX · tx_count = somme des eres · on note l'ERE d'ou vient le first_seen et la
presence par ere.

Sortie CSV : wallet, first_seen, first_seen_era, last_active, tx_count,
             on_gochain, on_imx, on_collectchain

Usage : python -m scraper.merge_wallets <dossier> <sortie.csv>
        (le dossier doit contenir les 3 CSV ci-dessus)
"""

from __future__ import annotations

import csv
import os
import sys

# (fichier, ere, colonne last_active, colonne tx) — deep/imx ont le meme schema,
# GoChain nomme differemment (last_seen / tx).
SOURCES = [
    ("gochain_wallets.csv",       "GoChain",      "last_seen",   "tx"),
    ("wallet_registry_imx.csv",   "IMX",          "last_active", "tx_count"),
    ("wallet_registry_deep.csv",  "CollectChain", "last_active", "tx_count"),
]


def _norm(w: str) -> str:
    return str(w or "").strip().lower()


def _int(x) -> int:
    try:
        return int(float(str(x)))
    except (TypeError, ValueError):
        return 0


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "regs"
    out = sys.argv[2] if len(sys.argv) > 2 else "wallets_full.csv"

    wallets: dict = {}          # wallet -> agrege
    for fname, era, col_last, col_tx in SOURCES:
        path = os.path.join(src, fname)
        if not os.path.exists(path):
            print(f"  ⚠️ {fname} absent — ere {era} ignoree.", file=sys.stderr)
            continue
        n = 0
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                w = _norm(r.get("wallet"))
                if not w or not w.startswith("0x"):
                    continue
                fs = str(r.get("first_seen") or "").strip()
                la = str(r.get(col_last) or "").strip()
                tx = _int(r.get(col_tx))
                d = wallets.get(w)
                if d is None:
                    d = {"first_seen": "", "first_seen_era": "", "last_active": "",
                         "tx_count": 0, "eras": set()}
                    wallets[w] = d
                d["eras"].add(era)
                d["tx_count"] += tx
                if fs and (not d["first_seen"] or fs < d["first_seen"]):
                    d["first_seen"] = fs           # le PLUS ANCIEN gagne
                    d["first_seen_era"] = era
                if la and la > d["last_active"]:
                    d["last_active"] = la          # le plus RECENT
                n += 1
        print(f"  {fname} ({era}) : {n} wallets lus (cumul uniques {len(wallets)})",
              flush=True)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    header = ["wallet", "first_seen", "first_seen_era", "last_active", "tx_count",
              "on_gochain", "on_imx", "on_collectchain"]
    og = 0
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for wal in sorted(wallets, key=lambda k: wallets[k]["first_seen"] or "9"):
            d = wallets[wal]
            if d["first_seen_era"] == "GoChain":
                og += 1
            w.writerow([wal, d["first_seen"], d["first_seen_era"], d["last_active"],
                        d["tx_count"],
                        int("GoChain" in d["eras"]), int("IMX" in d["eras"]),
                        int("CollectChain" in d["eras"])])
    print(f"✅ {out} : {len(wallets)} wallets — dont {og} avec un first_seen "
          f"GoChain (2020/2021), le reste IMX/CollectChain.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
