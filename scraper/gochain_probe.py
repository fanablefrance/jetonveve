"""SONDE GOCHAIN — l'ere d'AVANT IMX (2020 -> 14/12/2021).

CE QU'ON SAIT DEJA (etabli le 13/07 depuis le bac a sable) :
  * Le verdict "GoChain = impasse par API" etait FAUX : on cherchait des
    endpoints a la Etherscan (/api?module=) ou Blockscout (/api/v2/). L'explorer
    GoChain a SA PROPRE API REST, et elle repond.
  * LES ADRESSES SONT LES MEMES sur GoChain / IMX / CollectChain. Deux wallets
    actifs le 14/12/2021 (genese IMX) existent sur GoChain avec un solde GO et
    des token-transactions datees de MAI 2021. -> l'historique est JOIGNABLE au
    registre : c'etait LA question bloquante.
  * Le wallet utilisateur porte de l'OMI, pas des NFT : ses tx sont des
    transfer() d'OMI vers 0x17656848... = LA CAISSE de VeVe (817 574
    token-transactions, 0 tx normale).

CE QUE CETTE SONDE DOIT TRANCHER (et RIEN d'autre) :
  1. RPC   : le noeud public repond-il ? jusqu'ou porte un eth_getLogs ?
  2. DATES : quel bloc correspond a quelle date (ancre + vitesse de la chaine) ?
  3. NFT   : existait-il des NFT GoChain DANS les wallets users ? On ne devine
             pas : un Transfer ERC-20 a 3 topics, un Transfer ERC-721 en a 4.
             On balaie une fenetre de blocs et on CLASSE les contrats.
  4. CAISSE: combien de paiements OMI vers la caisse, et a quel rythme ?
             -> dimensionne la collecte complete (first_seen reel + revenue).

Aucune ecriture, aucun secret. Sortie = le log.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

RPC = os.environ.get("GO_RPC", "https://rpc.gochain.io")
EXPLORER = os.environ.get("GO_EXPLORER", "https://explorer.gochain.io")
OMI = "0x5347fdea6aa4d7770b31734408da6d34a8a07bdf"        # OMIToken (Go20)
CAISSE = "0x17656848e63cb846d93e629c710f6b0cc30a89dc"     # encaissements VeVe
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# ancre connue : bloc 18 850 837 = 2021-05-11T14:29:11Z (vu dans une tx reelle)
ANCRE_BLOC = int(os.environ.get("GO_ANCRE_BLOC", "18850837"))
STEPS = os.environ.get("GO_STEPS", "all").lower()
PAUSE = float(os.environ.get("GO_PAUSE", "0.3"))


def do(step: str) -> bool:
    return "all" in STEPS or step in STEPS


def _post(payload: dict, essais: int = 4):
    """JSON-RPC. Le sandbox ne peut pas faire de POST — un runner, si."""
    data = json.dumps(payload).encode()
    for i in range(essais):
        try:
            req = urllib.request.Request(
                RPC, data=data,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                out = json.loads(r.read())
            if "error" in out:
                return {"_erreur": out["error"]}
            return out.get("result")
        except (urllib.error.URLError, OSError, ValueError) as e:
            if i == essais - 1:
                return {"_erreur": str(e)}
            time.sleep(2 * (i + 1))
    return None


def _get(chemin: str, essais: int = 3):
    url = EXPLORER + chemin
    for i in range(essais):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_http": e.code}
        except (urllib.error.URLError, OSError, ValueError) as e:
            if i == essais - 1:
                return {"_erreur": str(e)}
            time.sleep(2 * (i + 1))
    return None


def rpc_hex(v) -> int:
    return int(v, 16) if isinstance(v, str) else -1


def bloc_ts(n: int):
    b = _post({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
               "params": [hex(n), False]})
    if not isinstance(b, dict) or "timestamp" not in b:
        return None
    return rpc_hex(b["timestamp"])


def iso(ts):
    import datetime as dt
    return (dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            if ts else "?")


# --------------------------------------------------------------------------
def etape_rpc():
    print("\n═══ 1. LE NOEUD RPC REPOND-IL ? ═══", flush=True)
    cid = _post({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId",
                 "params": []})
    tete = _post({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                  "params": []})
    print(f"  endpoint      : {RPC}")
    print(f"  eth_chainId   : {cid}")
    if not isinstance(tete, str):
        print(f"  eth_blockNumber : ECHEC -> {tete}")
        print("  ⛔ Sans RPC, il reste l'API REST de l'explorer (etape 4).")
        return None
    n = rpc_hex(tete)
    ts = bloc_ts(n)
    print(f"  tete de chaine: bloc {n} ({iso(ts)})")
    return n


def etape_dates(tete):
    print("\n═══ 2. QUEL BLOC POUR QUELLE DATE ? ═══", flush=True)
    ts_a = bloc_ts(ANCRE_BLOC)
    print(f"  ancre  : bloc {ANCRE_BLOC} -> {iso(ts_a)}")
    if not (tete and ts_a):
        return
    ts_t = bloc_ts(tete)
    secs = (ts_t - ts_a) / max(1, (tete - ANCRE_BLOC))
    print(f"  cadence moyenne depuis l'ancre : {secs:.2f} s/bloc "
          f"(~{86400 / secs:,.0f} blocs/jour)".replace(",", " "))
    # ou commencer / ou s'arreter, en supposant la cadence constante
    import datetime as dt
    for libelle, jour in (("genese VeVe (approx.)", "2020-01-01"),
                          ("migration IMX", "2021-12-14")):
        cible = dt.datetime.fromisoformat(jour).replace(
            tzinfo=dt.timezone.utc).timestamp()
        b = int(ANCRE_BLOC + (cible - ts_a) / secs)
        reel = bloc_ts(b) if b > 0 else None
        print(f"  {libelle:22} ~bloc {max(0, b):>10}  (verif : {iso(reel)})")
        time.sleep(PAUSE)
    print("  NB : cadence supposee constante -> a affiner par dichotomie dans "
          "le collecteur.")


def etape_logs(tete):
    print("\n═══ 3. JUSQU'OU PORTE UN eth_getLogs ? ═══", flush=True)
    if not tete:
        return
    for span in (100, 1000, 10000, 100000):
        d = max(0, ANCRE_BLOC)
        r = _post({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                   "params": [{"fromBlock": hex(d), "toBlock": hex(d + span),
                               "address": OMI, "topics": [TRANSFER]}]})
        if isinstance(r, list):
            print(f"  fenetre {span:>7} blocs : OK, {len(r)} logs OMI")
        else:
            print(f"  fenetre {span:>7} blocs : REFUSE -> {r}")
            print("  -> c'est la borne : le collecteur paginera par tranches "
                  "plus petites.")
            break
        time.sleep(PAUSE)


def etape_nft():
    """LA question : des NFT dans les wallets users, ou du custodial ?

    On ne devine pas : un Transfer ERC-20 porte 3 topics (sig, from, to) ;
    un Transfer ERC-721 en porte 4 (sig, from, to, tokenId indexe). On balaie
    une fenetre et on CLASSE les contrats par ce seul critere."""
    print("\n═══ 4. DES NFT GOCHAIN DANS LES WALLETS ? ═══", flush=True)
    d = ANCRE_BLOC
    span = int(os.environ.get("GO_NFT_SPAN", "20000"))   # ~1 jour de blocs
    r = _post({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
               "params": [{"fromBlock": hex(d), "toBlock": hex(d + span),
                           "topics": [TRANSFER]}]})
    if not isinstance(r, list):
        print(f"  balayage refuse -> {r}")
        return
    erc20, erc721 = {}, {}
    for lg in r:
        n = len(lg.get("topics") or [])
        a = (lg.get("address") or "").lower()
        (erc721 if n >= 4 else erc20)[a] = (
            (erc721 if n >= 4 else erc20).get(a, 0) + 1)
    print(f"  {len(r)} Transfer sur {span} blocs autour du {iso(bloc_ts(d))}")
    print(f"  contrats ERC-20  (3 topics) : {len(erc20)}")
    for a, c in sorted(erc20.items(), key=lambda x: -x[1])[:5]:
        marque = "  <-- OMI" if a == OMI else ""
        print(f"      {a}  {c:>6} transferts{marque}")
    print(f"  contrats ERC-721 (4 topics) : {len(erc721)}")
    for a, c in sorted(erc721.items(), key=lambda x: -x[1])[:8]:
        print(f"      {a}  {c:>6} transferts   <-- CANDIDAT NFT VEVE")
    if not erc721:
        print("      AUCUN. Si c'est confirme sur d'autres fenetres, les NFT")
        print("      GoChain n'etaient PAS dans les wallets users (custodial)")
        print("      -> GoChain ne donnera QUE le first_seen et le revenue OMI,")
        print("         pas la propriete des collectibles de l'epoque.")


def etape_caisse():
    print("\n═══ 5. LA CAISSE VEVE (paiements OMI des users) ═══", flush=True)
    info = _get(f"/api/address/{CAISSE}")
    if isinstance(info, dict) and "number_of_token_transactions" in info:
        print(f"  {CAISSE}")
        print(f"  token-transactions : "
              f"{info['number_of_token_transactions']:,}".replace(",", " "))
        print(f"  tx normales        : {info.get('number_of_transactions')}")
        print(f"  contrat            : {info.get('contract')}")
    else:
        print(f"  /api/address : {info}")
    # les paiements sont des Transfer OMI dont le topic 'to' est la caisse
    topic_to = "0x" + "0" * 24 + CAISSE[2:]
    d = ANCRE_BLOC
    span = int(os.environ.get("GO_CAISSE_SPAN", "20000"))
    r = _post({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
               "params": [{"fromBlock": hex(d), "toBlock": hex(d + span),
                           "address": OMI,
                           "topics": [TRANSFER, None, topic_to]}]})
    if isinstance(r, list):
        payeurs = {("0x" + lg["topics"][1][-40:]).lower() for lg in r}
        omi = sum(int(lg.get("data") or "0x0", 16) for lg in r) / 1e18
        print(f"  sur {span} blocs (~1 jour) : {len(r)} paiements, "
              f"{len(payeurs)} payeurs uniques, {omi:,.0f} OMI"
              .replace(",", " "))
        print("  -> filtre valide : le collecteur peut remonter toute l'ere "
              "GoChain avec ce seul appel, tranche par tranche.")
        for lg in r[:3]:
            print(f"     bloc {rpc_hex(lg['blockNumber'])} "
                  f"de 0x{lg['topics'][1][-40:]} "
                  f"{int(lg['data'], 16) / 1e18:,.0f} OMI".replace(",", " "))
    else:
        print(f"  getLogs caisse : {r}")


def main() -> int:
    print(f"SONDE GOCHAIN — steps={STEPS}", flush=True)
    print(f"  OMI    : {OMI}")
    print(f"  caisse : {CAISSE}")
    tete = etape_rpc() if do("rpc") or do("dates") or do("logs") else None
    if do("dates"):
        etape_dates(tete)
    if do("logs"):
        etape_logs(tete)
    if do("nft"):
        etape_nft()
    if do("caisse"):
        etape_caisse()
    print("\n═══ FIN ═══", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
