"""COLLECTEUR GOCHAIN — l'ere d'AVANT IMX (2020 -> migration du 14/12/2021).

CE QUE LA SONDE A ETABLI (13/07) :
  * 403 = l'User-Agent de Python (Cloudflare 1010). Des en-tetes de navigateur
    suffisent — RPC ET explorer repondent.
  * Les ADRESSES SONT LES MEMES sur GoChain / IMX / CollectChain -> tout ce
    qu'on collecte ici se raccorde au registre existant.
  * eth_getLogs accepte des fenetres de 100 000 blocs (49 601 logs d'un coup)
    -> les 12,3 M de blocs de l'ere GoChain = ~123 requetes. Pas un scan.
  * AUCUN contrat ERC-721 : les collectibles de 2021 etaient CUSTODIAL. On ne
    peut donc PAS reconstruire la propriete de l'epoque. Ce qu'on recupere :
      - le VRAI first_seen des wallets (2020/2021), aujourd'hui tous ecrases au
        14/12/2021 (genese IMX) -> la colonne OG devient exacte ;
      - le REVENUE de l'ere GoChain : les OMI payes a la caisse de VeVe.
  * cadence 5,01 s/bloc, tres stable (PoA) -> on date les logs en interpolant
    entre les bornes de chaque tranche (2 appels par tranche), au lieu de
    demander l'horodatage de chaque bloc.

SORTIES (commitees, lues ensuite par le repo preda comme les burns) :
  data/gochain_wallets.csv  wallet, first_seen, last_seen, tx, omi_in, omi_out,
                            paiements_veve, omi_paye
  data/gochain_daily.csv    date_pt, transferts, wallets_actifs, paiements,
                            payeurs_uniques, omi_paye
  data/gochain_state.json   reprise (cf. regle des collecteurs longs)

Aucun secret. Repo public.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

RPC = os.environ.get("GO_RPC", "https://rpc.gochain.io")
OMI = "0x5347fdea6aa4d7770b31734408da6d34a8a07bdf"
CAISSE = "0x17656848e63cb846d93e629c710f6b0cc30a89dc"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x0000000000000000000000000000000000000000"

# Bornes de l'ere (sondees) : la chaine demarre bien avant VeVe, on part du 1er
# bloc ou l'OMI existe et on s'arrete APRES la migration OMI vers Ethereum
# (janvier 2022) pour ne rien perdre de la queue.
DEBUT = int(os.environ.get("GO_DEBUT", "0"))
FIN = int(os.environ.get("GO_FIN", "24500000"))        # ~2022-03
TRANCHE = int(os.environ.get("GO_TRANCHE", "100000"))  # sonde : 100k accepte
MINUTES = float(os.environ.get("GO_MINUTES", "45"))    # budget par run
PAUSE = float(os.environ.get("GO_PAUSE", "0.15"))
FLUSH_TRANCHES = int(os.environ.get("GO_FLUSH", "10"))
# cap optionnel (tests, ou run court volontaire) : 0 = pas de limite
MAX_TRANCHES = int(os.environ.get("GO_MAX_TRANCHES", "0"))

# ── v2 (13/07) : LE REVENUE BRUT EST FAUX, ET IL FALLAIT LE VOIR ─────────────
# Le 1er run a rendu "85 732 915 346 OMI encaisses". Deux poisons dedans :
#   1. des MOUVEMENTS DE TRESORERIE deguises en paiements : 29 999 999 980 OMI
#      le 30/11/2020, 10 009 697 038 le 21/10/2020 — a eux deux, la MOITIE du
#      total. Aucun joueur n'achete pour 30 milliards d'OMI. (Meme piege que le
#      Golden MYO a 111 milliards de $ du flux VeVe : un garde-fou, jamais un
#      silence.)
#   2. a partir du 24/05/2021, les paiements des joueurs DISPARAISSENT de la
#      chaine : un wallet unique verse a la caisse ~24 fois par jour. VeVe est
#      passe a un encaissement CUSTODIAL. Ce flux EST du revenue (agrege), mais
#      "payeurs uniques = 1" n'a plus aucun sens.
# On ne peut pas trancher ca avec des totaux : il faut le DETAIL de chaque
# paiement. La v1 ne le gardait pas.
# → v2 : la marche ECRIT chaque paiement (data/gochain_paiements.csv), et le
#   CLASSEMENT devient une etape SEPAREE, rejouable a volonte sans re-marcher.
STEPS = os.environ.get("GO_STEPS", "all").lower()
OUT_P = os.environ.get("GO_OUT_PAIEMENTS", "data/gochain_paiements.csv")
# un paiement au-dela de ce seuil n'est pas un achat : c'est de la tresorerie.
# (au cours de 2021, 20 M OMI ~ 100 000 $ — deja hors de portee d'un joueur)
SEUIL_ABERRANT = float(os.environ.get("GO_SEUIL_ABERRANT", "20000000"))
# un payeur qui verse des centaines de fois n'est pas un client : c'est un
# automate. Detecte par les DONNEES, pas par une liste ecrite a la main.
SEUIL_INTERNE = int(os.environ.get("GO_SEUIL_INTERNE", "300"))

ETAT = os.environ.get("GO_STATE", "data/gochain_state.json")
OUT_W = os.environ.get("GO_OUT_WALLETS", "data/gochain_wallets.csv")
OUT_D = os.environ.get("GO_OUT_DAILY", "data/gochain_daily.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ENTETES = {"User-Agent": UA, "Accept": "application/json",
           "Content-Type": "application/json"}

PT = None
try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:                                    # pragma: no cover
    PT = dt.timezone(dt.timedelta(hours=-8))


def _rpc(method: str, params: list, essais: int = 6):
    """JSON-RPC avec backoff. Ne leve JAMAIS : rend None, l'appelant decide.
    (Regle des collecteurs longs : une panne transitoire ne doit pas jeter la
    recolte.)"""
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    for i in range(essais):
        try:
            req = urllib.request.Request(RPC, data=data, headers=ENTETES)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read())
            if "error" in out:
                return None
            return out.get("result")
        except (urllib.error.URLError, OSError, ValueError):
            if i == essais - 1:
                return None
            time.sleep(min(60, 3 * (2 ** i)))
    return None


def ts_bloc(n: int):
    b = _rpc("eth_getBlockByNumber", [hex(max(0, n)), False])
    if not isinstance(b, dict) or "timestamp" not in b:
        return None
    return int(b["timestamp"], 16)


def date_pt(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(
        PT).strftime("%Y-%m-%d")


def _adr(topic: str) -> str:
    return ("0x" + topic[-40:]).lower()


# ---------------------------------------------------------------------------
# Etat / sorties
# ---------------------------------------------------------------------------

def etape(step: str) -> bool:
    """Correspondance EXACTE (pas par sous-chaine : "nft" est dans "nft_ere")."""
    voulus = {x.strip() for x in STEPS.split(",") if x.strip()}
    return "all" in voulus or step in voulus


def charger_etat() -> dict:
    try:
        with open(ETAT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sauver_etat(e: dict) -> None:
    os.makedirs(os.path.dirname(ETAT) or ".", exist_ok=True)
    with open(ETAT, "w", encoding="utf-8") as f:
        json.dump(e, f, indent=1)


def charger_wallets() -> dict:
    w = {}
    try:
        with open(OUT_W, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                w[r["wallet"]] = {
                    "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                    "tx": int(r["tx"] or 0),
                    "omi_in": float(r["omi_in"] or 0),
                    "omi_out": float(r["omi_out"] or 0),
                    "paiements_veve": int(r["paiements_veve"] or 0),
                    "omi_paye": float(r["omi_paye"] or 0)}
    except (OSError, ValueError, KeyError):
        pass
    return w


def _jour_vide() -> dict:
    return {"transferts": 0, "actifs": set(), "paiements": 0,
            "payeurs": set(), "omi_paye": 0.0}


def charger_daily(etat: dict) -> dict:
    """Les jours COMPLETS viennent du CSV (compteurs figes, plus jamais
    touches). Le jour PARTIEL — celui a cheval sur la frontiere de tranche —
    est restitue depuis l'etat AVEC ses ensembles de wallets : sans ca, ses
    'wallets uniques' seraient recomptes a partir de zero apres une reprise et
    le total serait faux. (Meme patron que split_pending des burns.)"""
    d = {}
    try:
        with open(OUT_D, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d[r["date_pt"]] = {
                    "transferts": int(r["transferts"] or 0),
                    "actifs": set(), "fige_actifs": int(r["wallets_actifs"] or 0),
                    "paiements": int(r["paiements"] or 0),
                    "payeurs": set(),
                    "fige_payeurs": int(r["payeurs_uniques"] or 0),
                    "omi_paye": float(r["omi_paye"] or 0)}
    except (OSError, ValueError, KeyError):
        pass
    jp = etat.get("jour_partiel")
    if jp and jp.get("date"):
        d[jp["date"]] = {
            "transferts": jp.get("transferts", 0),
            "actifs": set(jp.get("actifs") or []),
            "paiements": jp.get("paiements", 0),
            "payeurs": set(jp.get("payeurs") or []),
            "omi_paye": jp.get("omi_paye", 0.0)}
    return d


def _n_uniques(d: dict, cle: str, fige: str) -> int:
    return d.get(fige, 0) if d.get(fige) else len(d[cle])


def ajouter_paiements(lignes) -> None:
    """Append-only : la recolte BRUTE, jamais perdue, jamais reecrite.
    C'est elle qui permet de RE-CLASSER sans re-marcher sur la chaine."""
    if not lignes:
        return
    neuf = not os.path.exists(OUT_P)
    os.makedirs(os.path.dirname(OUT_P) or ".", exist_ok=True)
    with open(OUT_P, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if neuf:
            w.writerow(["date_pt", "bloc", "payeur", "omi"])
        for l in lignes:
            w.writerow(l)


def ecrire(wallets: dict, daily: dict, partiel: str = "") -> None:
    """Ecrit les deux CSV. Le jour `partiel` (a cheval sur la frontiere de
    tranche) est EXCLU : il repart dans l'etat, ensembles compris, et sera
    ecrit quand il sera complet."""
    os.makedirs(os.path.dirname(OUT_W) or ".", exist_ok=True)
    with open(OUT_W, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["wallet", "first_seen", "last_seen", "tx", "omi_in",
                    "omi_out", "paiements_veve", "omi_paye"])
        for a in sorted(wallets):
            p = wallets[a]
            w.writerow([a, p["first_seen"], p["last_seen"], p["tx"],
                        round(p["omi_in"], 2), round(p["omi_out"], 2),
                        p["paiements_veve"], round(p["omi_paye"], 2)])
    with open(OUT_D, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date_pt", "transferts", "wallets_actifs", "paiements",
                    "payeurs_uniques", "omi_paye"])
        for j in sorted(daily):
            if j == partiel:
                continue
            d = daily[j]
            w.writerow([j, d["transferts"],
                        _n_uniques(d, "actifs", "fige_actifs"),
                        d["paiements"],
                        _n_uniques(d, "payeurs", "fige_payeurs"),
                        round(d["omi_paye"], 2)])


# ---------------------------------------------------------------------------
# Collecte
# ---------------------------------------------------------------------------

def _geler(daily: dict, jour: str):
    """Le jour partiel, ensembles compris, pour l'etat de reprise."""
    if not jour or jour not in daily:
        return None
    d = daily[jour]
    return {"date": jour, "transferts": d["transferts"],
            "actifs": sorted(d["actifs"]), "paiements": d["paiements"],
            "payeurs": sorted(d["payeurs"]), "omi_paye": d["omi_paye"]}


def logs_tranche(a: int, b: int, taille: int):
    """getLogs sur [a,b]. Si le noeud refuse, on DEGRADE la fenetre (regle des
    collecteurs longs) au lieu d'abandonner la tranche."""
    while True:
        r = _rpc("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(b),
                                  "address": OMI, "topics": [TRANSFER]}])
        if isinstance(r, list):
            return r, taille
        if taille <= 1000:
            print(f"    bloc {a}-{b} : refus persistant meme en {taille} "
                  f"blocs — tranche SAUTEE (notee dans l'etat).", flush=True)
            return None, taille
        taille //= 4
        b = min(b, a + taille)
        print(f"    refus -> fenetre reduite a {taille} blocs.", flush=True)


def collecter() -> int:
    t0 = time.time()
    etat = charger_etat()
    if etat.get("done"):
        print(f"Collecte GoChain deja terminee (jusqu'au bloc "
              f"{etat.get('bloc')}). Supprimer {ETAT} pour refaire.",
              flush=True)
        return 0

    bloc = int(etat.get("bloc") or DEBUT)
    sautees = etat.get("sautees") or []
    wallets = charger_wallets()
    daily = charger_daily(etat)
    print(f"GoChain : blocs {bloc} -> {FIN} (tranches de {TRANCHE}), "
          f"budget {MINUTES} min.", flush=True)
    print(f"  deja en base : {len(wallets)} wallets, {len(daily)} jours.",
          flush=True)

    n_logs = n_tranches = 0
    paiements: list = []
    ts_a = ts_bloc(bloc)
    while bloc <= FIN:
        if (time.time() - t0) / 60 >= MINUTES:
            print(f"  budget atteint — arret propre au bloc {bloc}.",
                  flush=True)
            break
        if MAX_TRANCHES and n_tranches >= MAX_TRANCHES:
            print(f"  cap de {MAX_TRANCHES} tranche(s) — arret au bloc {bloc}.",
                  flush=True)
            break
        fin = min(FIN, bloc + TRANCHE - 1)
        ts_b = ts_bloc(fin)
        if ts_a is None or ts_b is None or ts_b <= ts_a:
            ts_a = ts_a or (ts_b - 5 * TRANCHE if ts_b else None)
            if ts_a is None:
                print(f"  horodatage introuvable au bloc {bloc} — on saute.",
                      flush=True)
                sautees.append(bloc)
                bloc = fin + 1
                continue
            ts_b = ts_a + 5 * (fin - bloc)

        logs, _ = logs_tranche(bloc, fin, TRANCHE)
        if logs is None:
            sautees.append(bloc)
        else:
            n_logs += len(logs)
            pente = (ts_b - ts_a) / max(1, fin - bloc)   # ~5,01 s/bloc (PoA)
            for lg in logs:
                tp = lg.get("topics") or []
                if len(tp) < 3:
                    continue                # pas un Transfer ERC-20 standard
                nb = int(lg["blockNumber"], 16)
                jour = date_pt(int(ts_a + (nb - bloc) * pente))
                src, dst = _adr(tp[1]), _adr(tp[2])
                try:
                    val = int(lg.get("data") or "0x0", 16) / 1e18
                except ValueError:
                    val = 0.0
                d = daily.setdefault(jour, _jour_vide())
                d["transferts"] += 1
                for a, sens in ((src, "omi_out"), (dst, "omi_in")):
                    if a == ZERO:
                        continue
                    p = wallets.get(a)
                    if p is None:
                        p = wallets[a] = {
                            "first_seen": jour, "last_seen": jour, "tx": 0,
                            "omi_in": 0.0, "omi_out": 0.0,
                            "paiements_veve": 0, "omi_paye": 0.0}
                    if jour < p["first_seen"]:
                        p["first_seen"] = jour
                    if jour > p["last_seen"]:
                        p["last_seen"] = jour
                    p["tx"] += 1
                    p[sens] += val
                    d["actifs"].add(a)
                if dst == CAISSE and src != ZERO:      # versement a la caisse
                    d["paiements"] += 1
                    d["payeurs"].add(src)
                    d["omi_paye"] += val
                    wallets[src]["paiements_veve"] += 1
                    wallets[src]["omi_paye"] += val
                    paiements.append([jour, nb, src, round(val, 2)])

        n_tranches += 1
        bloc = fin + 1
        ts_a = ts_b
        if n_tranches % FLUSH_TRANCHES == 0:
            jp = max(daily) if daily else ""
            ajouter_paiements(paiements)
            paiements = []
            ecrire(wallets, daily, partiel=jp)
            sauver_etat({"bloc": bloc, "sautees": sautees, "done": False,
                         "logs": int(etat.get("logs", 0)) + n_logs,
                         "jour_partiel": _geler(daily, jp)})
            print(f"  ... bloc {bloc} ({date_pt(ts_a)}) : {n_logs} logs, "
                  f"{len(wallets)} wallets, {len(daily)} jours "
                  f"[sauvegarde]", flush=True)
        time.sleep(PAUSE)

    fini = bloc > FIN
    jp = "" if fini else (max(daily) if daily else "")
    ajouter_paiements(paiements)
    ecrire(wallets, daily, partiel=jp)
    sauver_etat({"bloc": bloc, "sautees": sautees, "done": fini,
                 "logs": int(etat.get("logs", 0)) + n_logs,
                 "jour_partiel": _geler(daily, jp)})
    print(f"\n{n_logs} logs sur {n_tranches} tranches. "
          f"TOTAL : {len(wallets)} wallets, {len(daily)} jours.", flush=True)
    if sautees:
        print(f"  ⚠️ {len(sautees)} tranche(s) sautee(s) : {sautees[:5]} — "
              f"relancer plus tard, l'etat les garde.", flush=True)
    if wallets:
        prem = min(p["first_seen"] for p in wallets.values())
        payeurs = sum(1 for p in wallets.values() if p["paiements_veve"])
        omi = sum(p["omi_paye"] for p in wallets.values())
        print(f"  1er wallet vu le {prem} · {payeurs} wallets ont paye VeVe · "
              f"{omi:,.0f} OMI encaisses.".replace(",", " "), flush=True)
    print("etat=" + ("TERMINE" if fini else f"REPRISE au bloc {bloc}"),
          flush=True)
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# CLASSEMENT — etape SEPAREE, rejouable sans re-marcher sur la chaine
# ═══════════════════════════════════════════════════════════════════════════

def classer() -> int:
    """Relit les paiements BRUTS et en tire un revenue defendable.

    TROIS natures, jamais melangees, jamais jetees :
      user      un joueur paie VeVe directement (l'ere ou c'etait on-chain)
      agrege    le wallet automate de VeVe verse a la caisse (custodial, a
                partir du 24/05/2021). C'EST du revenue — mais agrege : le
                nombre de payeurs n'y veut plus rien dire.
      aberrant  au-dela de SEUIL_ABERRANT : de la tresorerie, pas une vente.
                Comptee A PART, affichee, jamais silencieusement supprimee.

    revenue = user + agrege        (les aberrants restent visibles a cote)
    """
    if not os.path.exists(OUT_P):
        print(f"Pas de {OUT_P} — lance d'abord la collecte (elle ecrit les "
              f"paiements bruts).", flush=True)
        return 1
    lignes = []
    with open(OUT_P, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                lignes.append((r["date_pt"], r["payeur"], float(r["omi"])))
            except (KeyError, ValueError):
                continue
    print(f"Paiements bruts : {len(lignes)}", flush=True)
    if not lignes:
        return 1

    # 1) qui est un AUTOMATE ? On le demande aux donnees, pas a une liste.
    par_payeur: dict = defaultdict(lambda: [0, 0.0, "", ""])
    for j, p, v in lignes:
        e = par_payeur[p]
        e[0] += 1
        e[1] += v
        e[2] = j if not e[2] else min(e[2], j)
        e[3] = max(e[3], j)
    automates = {p for p, e in par_payeur.items() if e[0] >= SEUIL_INTERNE}
    print(f"\n  AUTOMATES (>= {SEUIL_INTERNE} versements) : {len(automates)}",
          flush=True)
    for p in sorted(automates, key=lambda x: -par_payeur[x][1]):
        n, omi, d1, d2 = par_payeur[p]
        print(f"    {p}  {n} versements · {omi:,.0f} OMI · {d1} -> {d2}"
              .replace(",", " "), flush=True)

    # 2) les gros clients LEGITIMES, pour verifier qu'on ne coupe pas trop haut
    print(f"\n  TOP 10 des payeurs NON automates :", flush=True)
    for p in sorted((x for x in par_payeur if x not in automates),
                    key=lambda x: -par_payeur[x][1])[:10]:
        n, omi, d1, d2 = par_payeur[p]
        print(f"    {p}  {n} versements · {omi:,.0f} OMI · {d1} -> {d2}"
              .replace(",", " "), flush=True)

    # 3) classement
    jours: dict = {}
    aberrants = []
    for j, p, v in lignes:
        d = jours.setdefault(j, {"pu": 0, "ou": 0.0, "payeurs": set(),
                                 "pa": 0, "oa": 0.0, "pb": 0, "ob": 0.0})
        if v >= SEUIL_ABERRANT:
            d["pb"] += 1
            d["ob"] += v
            aberrants.append((j, p, v))
        elif p in automates:
            d["pa"] += 1
            d["oa"] += v
        else:
            d["pu"] += 1
            d["ou"] += v
            d["payeurs"].add(p)

    tot_u = sum(d["ou"] for d in jours.values())
    tot_a = sum(d["oa"] for d in jours.values())
    tot_b = sum(d["ob"] for d in jours.values())
    print(f"\n  ABERRANTS (>= {SEUIL_ABERRANT:,.0f} OMI) : {len(aberrants)}"
          .replace(",", " "), flush=True)
    for j, p, v in sorted(aberrants, key=lambda x: -x[2])[:12]:
        print(f"    {j}  {v:,.0f} OMI  de {p}".replace(",", " "), flush=True)

    print(f"\n  ═══ REVENUE GOCHAIN ═══", flush=True)
    print(f"    joueurs (on-chain)  : {tot_u:,.0f} OMI".replace(",", " "))
    print(f"    agrege (custodial)  : {tot_a:,.0f} OMI".replace(",", " "))
    print(f"    ─────────────────────────────────")
    print(f"    REVENUE             : {tot_u + tot_a:,.0f} OMI"
          .replace(",", " "))
    print(f"    tresorerie (exclue) : {tot_b:,.0f} OMI".replace(",", " "))
    brut = tot_u + tot_a + tot_b
    if brut:
        print(f"    -> le brut annoncait {brut:,.0f} OMI : "
              f"{100 * tot_b / brut:.0f} % n'etaient PAS des ventes."
              .replace(",", " "), flush=True)

    # 4) le CSV propre
    os.makedirs(os.path.dirname(OUT_D) or ".", exist_ok=True)
    anciens = {}
    try:
        with open(OUT_D, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                anciens[r["date_pt"]] = r
    except (OSError, KeyError):
        pass
    with open(OUT_D, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date_pt", "transferts", "wallets_actifs",
                    "paiements_user", "payeurs_uniques", "omi_user",
                    "paiements_agrege", "omi_agrege", "omi_revenue",
                    "paiements_tresorerie", "omi_tresorerie"])
        for j in sorted(set(jours) | set(anciens)):
            d = jours.get(j) or {"pu": 0, "ou": 0.0, "payeurs": set(),
                                 "pa": 0, "oa": 0.0, "pb": 0, "ob": 0.0}
            a = anciens.get(j, {})
            w.writerow([j, a.get("transferts", ""), a.get("wallets_actifs", ""),
                        d["pu"], len(d["payeurs"]), round(d["ou"], 2),
                        d["pa"], round(d["oa"], 2),
                        round(d["ou"] + d["oa"], 2),
                        d["pb"], round(d["ob"], 2)])
    print(f"\n  {OUT_D} reecrit : {len(jours)} jour(s) classes.", flush=True)
    print("\n  ⚠️ A RETENIR : `payeurs_uniques` ne veut RIEN dire apres le "
          "24/05/2021", flush=True)
    print("     (encaissement custodial : un seul automate verse pour tous).",
          flush=True)
    return 0


def main() -> int:
    if etape("collecte"):
        r = collecter()
        if r:
            return r
    if etape("classer"):
        return classer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
