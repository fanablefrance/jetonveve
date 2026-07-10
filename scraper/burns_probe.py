"""
Sonde BURNS OMI — diagnostic des sources avant de batir le tracker.

Interroge, avec la cle Etherscan v2 (multichain ETH+Base) et l'explorer Gochain
(Blockscout, sans cle), les adresses de burn VeVe et StackR, et affiche des
echantillons de transferts pour comprendre la structure (d'ou viennent les OMI,
comment separer NFT/GEM, ou vont-ils). N'ecrit RIEN. Coller le log dans le chat.

Env : ETHERSCAN_API_KEY.
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

ETHERSCAN = "https://api.etherscan.io/v2/api"
GOCHAIN = "https://explorer.gochain.io/api"

OMI_ETH = "0xed35af169af46a02ee13b9d79eb57d6d68c1749e"        # OMI ERC-20 sur Ethereum
STACKR_TOKEN_BASE = "0x3792DBDD07e87413247DF995e692806aa13D3299"  # jeton StackR sur Base
VEVE_BURN = "0xbbda162f1e3ec2d4d9d99cafd0c14b03ec4e78d3"       # burn VeVe (Gochain + ETH)
STACKR_ADDR = "0x821c1ed723c3148eb74540b1201ea3369c910c17"     # StackR (Base + ETH)

KEY = os.environ.get("ETHERSCAN_API_KEY", "").strip()


def es(chainid, params):
    p = {"chainid": chainid, "apikey": KEY, **params}
    r = requests.get(ETHERSCAN, params=p, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"status": "0", "message": "non-JSON", "result": r.text[:200]}


def show(title, j, keys=("from", "to", "value", "tokenSymbol", "tokenName",
                         "timeStamp", "hash")):
    print(f"\n=== {title}", flush=True)
    if isinstance(j, dict):
        print(f"   status={j.get('status')} message={j.get('message')}", flush=True)
        res = j.get("result")
        if isinstance(res, list):
            print(f"   {len(res)} resultats. Echantillon :", flush=True)
            for row in res[:4]:
                if isinstance(row, dict):
                    print("   " + " | ".join(f"{k}={row.get(k)}" for k in keys
                                             if k in row), flush=True)
        else:
            print(f"   result={str(res)[:300]}", flush=True)


def main() -> int:
    if not KEY:
        print("ERREUR : secret ETHERSCAN_API_KEY absent.", file=sys.stderr)
        return 2
    print(f"Cle Etherscan presente ({KEY[:4]}...). Sondage...", flush=True)

    # --- Ethereum L1 : OMI vers/depuis les adresses de burn ---
    show("ETH L1 — OMI transferts @ VeVe burn (0xbbda)",
         es(1, {"module": "account", "action": "tokentx", "contractaddress": OMI_ETH,
                "address": VEVE_BURN, "page": 1, "offset": 5, "sort": "desc"}))
    time.sleep(0.25)
    show("ETH L1 — solde OMI detenu par VeVe burn (0xbbda)",
         es(1, {"module": "account", "action": "tokenbalance", "contractaddress": OMI_ETH,
                "address": VEVE_BURN, "tag": "latest"}))
    time.sleep(0.25)
    show("ETH L1 — OMI transferts @ StackR (0x821c)",
         es(1, {"module": "account", "action": "tokentx", "contractaddress": OMI_ETH,
                "address": STACKR_ADDR, "page": 1, "offset": 5, "sort": "desc"}))
    time.sleep(0.25)
    show("ETH L1 — solde OMI detenu par StackR (0x821c)",
         es(1, {"module": "account", "action": "tokenbalance", "contractaddress": OMI_ETH,
                "address": STACKR_ADDR, "tag": "latest"}))
    time.sleep(0.25)

    # --- Base L2 : jeton StackR vers/depuis 0x821c ---
    show("BASE L2 — jeton StackR (0x3792) @ 0x821c",
         es(8453, {"module": "account", "action": "tokentx",
                   "contractaddress": STACKR_TOKEN_BASE, "address": STACKR_ADDR,
                   "page": 1, "offset": 5, "sort": "desc"}))
    time.sleep(0.25)
    # tous jetons recus par 0x821c sur Base (pour voir ce qui circule)
    show("BASE L2 — tous jetons @ 0x821c",
         es(8453, {"module": "account", "action": "tokentx", "address": STACKR_ADDR,
                   "page": 1, "offset": 5, "sort": "desc"}))
    time.sleep(0.25)

    # --- Gochain (Blockscout, sans cle) : burns VeVe natifs ---
    print("\n=== GOCHAIN — tokentx @ VeVe burn (0xbbda)", flush=True)
    try:
        r = requests.get(GOCHAIN, params={"module": "account", "action": "tokentx",
                         "address": VEVE_BURN, "page": 1, "offset": 5, "sort": "desc"},
                         timeout=30)
        j = r.json()
        res = j.get("result")
        print(f"   status={j.get('status')} n={len(res) if isinstance(res,list) else res}",
              flush=True)
        if isinstance(res, list):
            for row in res[:4]:
                print("   " + " | ".join(f"{k}={row.get(k)}" for k in
                      ("from", "to", "value", "tokenSymbol", "timeStamp") if k in row),
                      flush=True)
    except Exception as e:
        print(f"   Gochain ERR : {e}", flush=True)

    print("\nSondage termine. Colle ce log dans le chat.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
