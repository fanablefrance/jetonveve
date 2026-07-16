"""
Wallet registry — enriches the `Pseudos` tab with on-chain activity per wallet.

Turns `Pseudos` into a wallet catalogue: for every row that has a CollectChain
wallet (`wallet_imx`), it fills 2 columns from ChainActivity:

    chain_first_seen    earliest day the wallet was active (kept persistent:
                        only ever moves EARLIER, so it survives the 35-day
                        ChainActivity pruning and approximates wallet creation)
    chain_last_active   most recent active day

Serves: "wallets created this month + do they keep going".
Runs LAST in the daily workflow (after chain + stackr + market) so it sees the
final Pseudos. Only enriches KNOWN wallets — unknown wallets stay in
ChainActivity (raw) and can be surfaced later.

Env: GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List

from scraper.sheets import _client, _open_worksheet, append_log
from scraper.chain_sheets import read_activity
from scraper.stackr import (PSEUDOS_TAB, PSEUDOS_HEADER, apply_type_validation,
                            apply_type_colors)


def _aggregate(activity: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for r in activity:
        acc = str(r.get("account", "")).strip().lower()
        if not acc:
            continue
        d = str(r.get("date", "")).strip()
        a = agg.get(acc)
        if a is None:
            a = agg[acc] = {"first": d, "last": d, "days": set(),
                            "mints": 0, "buys": 0, "sells": 0}
        if d:
            a["days"].add(d)
            if not a["first"] or d < a["first"]:
                a["first"] = d
            if d > a["last"]:
                a["last"] = d
        a["mints"] += int(r.get("mint_collectible", 0) or 0) + int(r.get("mint_comic", 0) or 0)
        a["buys"] += int(r.get("market_in_collectible", 0) or 0) + int(r.get("market_in_comic", 0) or 0)
        a["sells"] += int(r.get("market_out_collectible", 0) or 0) + int(r.get("market_out_comic", 0) or 0)
    return agg


def _earliest(a: str, b: str) -> str:
    vals = [x for x in (str(a or "").strip(), str(b or "").strip()) if x]
    return min(vals) if vals else ""


def main() -> int:
    t0 = time.time()
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        print("ERROR: SHEET_ID env var is required.", file=sys.stderr)
        return 2

    activity = read_activity(sheet_id)
    agg = _aggregate(activity)
    print(f"Aggregated activity for {len(agg)} wallets from ChainActivity.", flush=True)

    sh = _client().open_by_key(sheet_id)
    ws = _open_worksheet(sh, PSEUDOS_TAB, cols=len(PSEUDOS_HEADER))
    rows = ws.get_all_records() if ws.row_count > 1 else []
    if not rows:
        print("Pseudos empty — nothing to enrich.", flush=True)
        return 0

    updated = 0
    for r in rows:
        w = str(r.get("wallet_imx", "")).strip().lower()
        a = agg.get(w)
        if not a:
            continue
        r["chain_first_seen"] = _earliest(r.get("chain_first_seen", ""), a["first"])
        r["chain_last_active"] = max(str(r.get("chain_last_active", "")).strip(), a["last"])
        updated += 1

    grid = [PSEUDOS_HEADER] + [[r.get(c, "") for c in PSEUDOS_HEADER] for r in rows]
    ws.clear()
    for i in range(0, len(grid), 20000):
        if i == 0:
            ws.update(range_name="A1", values=grid[:20000], value_input_option="RAW")
        else:
            ws.append_rows(grid[i:i + 20000], value_input_option="RAW")
    try:
        ws.freeze(rows=1, cols=2)      # username + Type de compte figes (Preda)
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass
    # (re)pose le menu deroulant + le fond par type sur la colonne manuelle
    apply_type_validation(sh, ws)
    apply_type_colors(sh, ws)

    summary = {"status": "OK", "wallets_active": len(agg),
               "pseudos_enriched": updated, "duration": f"{time.time()-t0:.0f}s"}
    try:
        append_log(sheet_id, "wallets", "OK",
                   "; ".join(f"{k}={v}" for k, v in summary.items() if k != "status"))
    except Exception as e:
        print(f"log warning: {e}", flush=True)
    print(f"Done. {summary}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
