"""
Sonde BURNS OMI v3 — debloquer GOCHAIN (burns VeVe) + confirmer le cumul StackR.

StackR (Base L2) : OK via base.blockscout.com. Reste GOCHAIN pour VeVe : on teste
plusieurs endpoints (txlist natif, tokenlist, tokentx, stats) car OMI y est
peut-etre le jeton NATIF (burns = transactions natives, pas token-transfers).

N'ecrit rien.
"""

from __future__ import annotations

import sys
import time

import requests

VEVE_BURN = "0xbbda162f1e3ec2d4d9d99cafd0c14b03ec4e78d3"
STACKR_ADDR = "0x821c1ed723c3148eb74540b1201ea3369c910c17"
GOCHAIN = "https://explorer.gochain.io"
BASE_BS = "https://base.blockscout.com"
UA = {"User-Agent": "veve-omi-probe/1.0", "Accept": "application/json"}


def show(label, url, params=None):
    print(f"\n=== {label}\n   {url} {params or ''}", flush=True)
    try:
        r = requests.get(url, params=params or {}, headers=UA, timeout=40)
        ct = r.headers.get("content-type", "")
        print(f"   HTTP {r.status_code} ({ct})", flush=True)
        txt = r.text
        if "json" in ct:
            j = r.json()
            res = j.get("result") if isinstance(j, dict) else j
            if isinstance(res, list):
                print(f"   result: liste de {len(res)}. 1er: {str(res[0])[:280] if res else '-'}",
                      flush=True)
            else:
                print(f"   json: {str(j)[:300]}", flush=True)
            return j
        print(f"   raw[:220]: {txt[:220]!r}", flush=True)
    except Exception as e:
        print(f"   ERR {e}", flush=True)
    return None


def main() -> int:
    # --- GOCHAIN : plusieurs endpoints candidats ---
    show("GOCHAIN txlist natif @ VeVe burn",
         f"{GOCHAIN}/api", {"module": "account", "action": "txlist",
                            "address": VEVE_BURN, "page": 1, "offset": 5, "sort": "desc"})
    time.sleep(0.3)
    show("GOCHAIN tokenlist @ VeVe burn",
         f"{GOCHAIN}/api", {"module": "account", "action": "tokenlist",
                            "address": VEVE_BURN})
    time.sleep(0.3)
    show("GOCHAIN balance native @ VeVe burn",
         f"{GOCHAIN}/api", {"module": "account", "action": "balance",
                            "address": VEVE_BURN})
    time.sleep(0.3)
    show("GOCHAIN stats coinsupply", f"{GOCHAIN}/api",
         {"module": "stats", "action": "coinsupply"})
    time.sleep(0.3)
    # v2 blockscout alternatif (transactions)
    show("GOCHAIN v2 transactions @ VeVe burn",
         f"{GOCHAIN}/api/v2/addresses/{VEVE_BURN}/transactions")
    time.sleep(0.3)

    # --- BASE StackR : cumul + counters (pour le total burn StackR) ---
    show("BASE StackR counters",
         f"{BASE_BS}/api/v2/addresses/{STACKR_ADDR}/counters")
    time.sleep(0.3)
    show("BASE StackR info (coin_balance, etc.)",
         f"{BASE_BS}/api/v2/addresses/{STACKR_ADDR}")

    print("\nSonde v3 terminee. Colle le log.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
