"""
Sonde BURNS OMI v2 — cible les explorers Blockscout OUVERTS (Gochain + Base),
puisque Etherscan gratuit ne couvre pas Base et que le suivi quotidien NFT/GEM
vit sur Gochain (chaine native VeVe). ETH L1 garde juste un role de reference
(gros burns periodiques de consolidation).

Burns = transferts vers 0x0. Les adresses de burn (0xbbda VeVe, 0x821c StackR)
ont un solde nul (transit). On veut : d'ou viennent les depots (separer NFT/GEM)
et les burns -> 0x0 datables au jour.

N'ecrit rien. Env : ETHERSCAN_API_KEY (pour la partie ETH L1 de reference).
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

DEAD = "0x0000000000000000000000000000000000000000"
OMI_ETH = "0xed35af169af46a02ee13b9d79eb57d6d68c1749e"
VEVE_BURN = "0xbbda162f1e3ec2d4d9d99cafd0c14b03ec4e78d3"
STACKR_ADDR = "0x821c1ed723c3148eb74540b1201ea3369c910c17"
STACKR_TOKEN_BASE = "0x3792dbdd07e87413247df995e692806aa13d3299"

GOCHAIN = "https://explorer.gochain.io"
BASE_BS = "https://base.blockscout.com"
UA = {"User-Agent": "veve-omi-probe/1.0", "Accept": "application/json"}


def get(url, params=None):
    try:
        r = requests.get(url, params=params or {}, headers=UA, timeout=40)
        ct = r.headers.get("content-type", "")
        print(f"   HTTP {r.status_code} ({ct})", flush=True)
        if "json" in ct:
            return r.json()
        print(f"   raw: {r.text[:200]}", flush=True)
        return None
    except Exception as e:
        print(f"   ERR {e}", flush=True)
        return None


def blockscout_transfers(base, addr, label):
    """Blockscout v2 : transferts de jetons d'une adresse (entrants + sortants)."""
    print(f"\n=== {label} — Blockscout v2 token-transfers @ {addr}", flush=True)
    j = get(f"{base}/api/v2/addresses/{addr}/token-transfers", {"type": "ERC-20"})
    if not j:
        # fallback API v1
        print("   (fallback API v1 tokentx)", flush=True)
        j = get(f"{base}/api", {"module": "account", "action": "tokentx",
                                "address": addr, "page": 1, "offset": 5, "sort": "desc"})
        if isinstance(j, dict) and isinstance(j.get("result"), list):
            for r in j["result"][:5]:
                print("   " + " | ".join(f"{k}={r.get(k)}" for k in
                      ("from", "to", "value", "tokenSymbol", "timeStamp") if k in r),
                      flush=True)
        return
    items = j.get("items") or []
    print(f"   {len(items)} transferts. Echantillon :", flush=True)
    froms = {}
    for it in items[:10]:
        frm = ((it.get("from") or {}).get("hash") or "")[:12]
        to = ((it.get("to") or {}).get("hash") or "")[:12]
        tok = (it.get("token") or {}).get("symbol")
        val = it.get("total", {}).get("value") if isinstance(it.get("total"), dict) else it.get("value")
        ts = it.get("timestamp")
        print(f"   from={frm} to={to} tok={tok} val={val} ts={ts}", flush=True)
        froms[frm] = froms.get(frm, 0) + 1
    print(f"   sources distinctes (from) dans l'echantillon : {froms}", flush=True)


def main() -> int:
    key = os.environ.get("ETHERSCAN_API_KEY", "").strip()

    # --- GOCHAIN (coeur du suivi quotidien NFT/GEM) ---
    blockscout_transfers(GOCHAIN, VEVE_BURN, "GOCHAIN VeVe burn")
    time.sleep(0.3)
    # les transactions natives de l'adresse (pour voir le rythme quotidien)
    print(f"\n=== GOCHAIN VeVe burn — counters @ {VEVE_BURN}", flush=True)
    get(f"{GOCHAIN}/api/v2/addresses/{VEVE_BURN}/counters")
    time.sleep(0.3)

    # --- BASE L2 (StackR) via Blockscout ouvert ---
    blockscout_transfers(BASE_BS, STACKR_ADDR, "BASE StackR")
    time.sleep(0.3)

    # --- ETH L1 reference (Etherscan) : les burns -> 0x0 ---
    if key:
        print("\n=== ETH L1 (reference) — burns OMI -> 0x0 depuis 0xbbda", flush=True)
        r = requests.get("https://api.etherscan.io/v2/api",
                         params={"chainid": 1, "module": "account", "action": "tokentx",
                                 "contractaddress": OMI_ETH, "address": VEVE_BURN,
                                 "page": 1, "offset": 10, "sort": "desc", "apikey": key},
                         headers=UA, timeout=30)
        try:
            res = r.json().get("result", [])
            burns = [x for x in res if isinstance(x, dict) and x.get("to") == DEAD]
            print(f"   {len(burns)} burns->0x0 dans les 10 derniers mouvements.", flush=True)
            for x in burns[:3]:
                omi = int(x.get("value", "0")) / 1e18
                print(f"   {omi:,.0f} OMI  ts={x.get('timeStamp')}  tx={x.get('hash')[:14]}",
                      flush=True)
        except Exception as e:
            print(f"   ERR {e}", flush=True)

    print("\nSonde v2 terminee. Colle le log dans le chat.", flush=True)
    return 0


if __name__ == "__main__":
    sys