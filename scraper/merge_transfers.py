"""🧬 merge_transfers — les transferts NFT VeVe, IMX + CollectChain, en un fichier.

But (demande Preda) : une source unifiee des transferts NFT sur les deux eres
on-chain qui comptent (IMX 2021->2026, CollectChain 2026->). GoChain est exclu
(NFT custodial = aucun transfert par wallet).

Le probleme : les deux archives n'ont PAS le meme schema.
  - CollectChain : block,log_index,ts_utc,date_pt,kind,category,veve_uuid,edition,from,to
  - IMX          : txn_id,txn_time_ms,date_pt,txn_type,from,to,token_id,token_address
L'IMX n'a que `token_id`. On le mappe vers veve_uuid/category/edition grace au
HOLDERS-SNAPSHOT (paolo), qui porte token_id -> veve_uuid/category/edition.

Schema unifie de sortie :
    era,ts_utc,date_pt,kind,category,veve_uuid,edition,from,to,tx_ref

Dedup : IMX par txn_id · CollectChain par (block,log_index). Les eras ne se
CHEVAUCHENT pas (IMX < 2026-01-28 <= CollectChain) -> on ecrit l'IMX trie puis
CollectChain trie = deja chronologique, sans tri croisé (et sans tout charger).

Entree : un dossier avec 3 sous-dossiers (pour ne pas melanger les schemas) :
    <src>/holders/*.csv*   holders-snapshot (token_id -> veve_uuid)
    <src>/imx/*.csv*       imx-archive (paolo)
    <src>/cc/*.csv*        chain-archive (astronema) + chain-archive-daily (preda)
Usage : python -m scraper.merge_transfers <dossier> <sortie.csv.gz>
"""

from __future__ import annotations

import csv
import datetime as _dt
import glob
import gzip
import os
import sys

OUT_HEADER = ("era,ts_utc,date_pt,kind,category,veve_uuid,edition,from,to,tx_ref")


def _int(x) -> int:
    try:
        return int(float(str(x)))
    except (TypeError, ValueError):
        return 0


def _open(path):
    return (gzip.open(path, "rt", encoding="utf-8", newline="")
            if path.endswith(".gz")
            else open(path, encoding="utf-8", newline=""))


def _ms_to_iso(ms) -> str:
    m = _int(ms)
    if m <= 0:
        return ""
    try:
        return _dt.datetime.utcfromtimestamp(m / 1000.0).strftime(
            "%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _csv_field(v: str) -> str:
    """Echappe un champ pour un CSV ecrit a la main (les noms/uuid n'ont pas de
    virgule, mais on securise le kind/txn_type au cas ou)."""
    s = str(v or "")
    if "," in s or '"' in s or "\n" in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def _charger_map(src: str) -> dict:
    """token_id -> (veve_uuid, category, edition) depuis le(s) holders-snapshot."""
    m: dict = {}
    files = sorted(glob.glob(os.path.join(src, "holders", "*.csv*")))
    for f in files:
        with _open(f) as fh:
            for r in csv.DictReader(fh):
                tid = str(r.get("token_id") or "").strip()
                if tid and tid not in m:
                    m[tid] = (str(r.get("veve_uuid") or ""),
                              str(r.get("category") or ""),
                              str(r.get("edition") or ""))
    print(f"  holders-snapshot : {len(m)} token_id -> veve_uuid "
          f"({len(files)} fichier(s)).", flush=True)
    return m


def _ere_imx(src: str, tok: dict, out) -> int:
    """Ecrit l'ere IMX (deja habillee + dedup par txn_id + triee par temps)."""
    rows: dict = {}                 # txn_id -> (ts_ms, ligne_unifiee)
    files = sorted(glob.glob(os.path.join(src, "imx", "*.csv*")))
    for f in files:
        with _open(f) as fh:
            for r in csv.DictReader(fh):
                tid = str(r.get("txn_id") or "").strip()
                if not tid or tid in rows:
                    continue
                token = str(r.get("token_id") or "").strip()
                vu, cat, ed = tok.get(token, ("", "", ""))
                ts = _ms_to_iso(r.get("txn_time_ms"))
                champs = ["IMX", ts, str(r.get("date_pt") or ""),
                          str(r.get("txn_type") or "").lower(), cat, vu, ed,
                          str(r.get("from") or ""), str(r.get("to") or ""),
                          "imx:" + tid]
                rows[tid] = (_int(r.get("txn_time_ms")),
                             ",".join(_csv_field(c) for c in champs))
    for _, line in sorted(rows.values(), key=lambda kv: kv[0]):
        out.write(line + "\n")
    print(f"  IMX : {len(rows)} transferts ({len(files)} fichier(s)).", flush=True)
    return len(rows)


def _ere_cc(src: str, out) -> int:
    """Ecrit l'ere CollectChain (dedup par (block,log_index), triee)."""
    rows: dict = {}                 # (block, log_index) -> ligne unifiee
    files = sorted(glob.glob(os.path.join(src, "cc", "*.csv*")))
    for f in files:
        with _open(f) as fh:
            fh.readline()           # entete
            for line in fh:
                p = line.rstrip("\n").rstrip("\r").split(",")
                if len(p) < 10:
                    continue
                key = (_int(p[0]), _int(p[1]))
                if key in rows:
                    continue
                # p = block,log_index,ts_utc,date_pt,kind,category,veve_uuid,edition,from,to
                champs = ["CollectChain", p[2], p[3], p[4], p[5], p[6], p[7],
                          p[8], p[9], "cc:" + p[0] + ":" + p[1]]
                rows[key] = ",".join(_csv_field(c) for c in champs)
    for key in sorted(rows):
        out.write(rows[key] + "\n")
    print(f"  CollectChain : {len(rows)} transferts ({len(files)} fichier(s)).",
          flush=True)
    return len(rows)


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "arch"
    out = sys.argv[2] if len(sys.argv) > 2 else "transfers_full.csv.gz"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    tok = _charger_map(src)
    with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
        fh.write(OUT_HEADER + "\n")
        n_imx = _ere_imx(src, tok, fh)   # IMX d'abord (plus ancien)
        tok.clear()                      # libere la table de mapping
        n_cc = _ere_cc(src, fh)          # puis CollectChain
    taille = os.path.getsize(out) / (1024 * 1024)
    print(f"✅ {out} : {n_imx + n_cc} transferts (IMX {n_imx} -> CollectChain "
          f"{n_cc}), {taille:.1f} Mo.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
