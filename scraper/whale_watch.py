# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/whale_watch.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🐋 SUIVI DES COMPTES WHALE / VeVe TEAM — canal Discord dedie.

POURQUOI CE MODULE
------------------
Preda tague certains comptes dans l'onglet 🟣C-PSEUDOS (colonne « Type de
compte » : VeVe Team / Fondateur / Moderation / Publisher / Influenceur / A
suivre). On veut voir CE QUE CES COMPTES FONT, en direct, dans un canal a part.

LES TROIS EVENEMENTS (choix de Preda)
-------------------------------------
  🛒 ACHAT            — un compte suivi ACHETE (flux ventes StackR, buyer).
  💸 VENTE / 🏷️ MISE EN VENTE — il vend ou liste (flux ventes/listings StackR).
  🔀 GROS TRANSFERT   — un mouvement NFT hors marche (wallet-a-wallet) d'au
                        moins WHALE_XFER_MIN jetons dans une meme transaction,
                        lu sur CollectChain (collectscan). On EXCLUT l'escrow du
                        marche (deja couvert par 🛒/💸/🏷️), le mint (from = 0x0)
                        et le burn : ne restent que les vrais transferts entre
                        wallets (cadeau, consolidation, OTC).

LE PONT (comme elements.csv / comics_supply.csv)
------------------------------------------------
Le tag n'existe QUE dans le Sheet, donc chez preda (PRIVE). preda exporte les
lignes taguees dans `data/tracked_accounts.csv` (`export_tracked.py`), jetonveve
le lit en sparse-checkout (`_preda/data/tracked_accounts.csv`). Sans le pont, le
module se tait poliment (aucun compte suivi) — le reste des alertes n'est pas
touche.

ARCHITECTURE
------------
On REUTILISE le moteur de `floor_watch` (memes appels StackR, meme cours OMI,
meme garde-fou anti-ban `budget`/`consommer` a 20 msg/min par webhook) : zero
requete StackR en plus, zero regle de mise en forme dupliquee. Les evenements
marche viennent du flux (2 min = instantane) ; les gros transferts on-chain
sont releves 1x/h (collectscan est un explorateur public, on reste poli).

Anti-bruit : chaque evenement marche est dedoublonne par (genre, nft_id,
timestamp) ; chaque transfert par hash de tx.

DEBORDEMENT — CORRIGE LE 20/07/2026, VU DANS UN LOG DE PROD
-----------------------------------------------------------
L'ancienne regle etait « au-dela de WHALE_MAX d'un coup, on ne publie RIEN et
on ne MEMORISE RIEN ». Elle a rendu ce module MUET :

    ⛔ 26 evenements comptes suivis d'un coup — anormal. RIEN publie ni memorise.

repete a l'identique 25 fois par run, indefiniment. Sans memorisation, les
memes 26 evenements etaient redecouverts, recomptes, rejetes au tour suivant —
et le compteur ne redescendait JAMAIS sous le plafond. 🐋 ne publiait plus rien.

⭐ LA REPETITION A L'IDENTIQUE DANS UN LOG EST LA SIGNATURE DE CE DEFAUT : un
vrai pic de marche varie d'un tour a l'autre. A chercher dans les autres logs.

La regle correcte distingue le TYPE de detecteur (lecon de 📊/🔊, 20/07) :
  · 📉 ATL est un detecteur de TRANSITION — un debordement y signale une
    RECOLTE ABERRANTE, jeter le lot est le bon reflexe.
  · 🐋 est un detecteur d'EVENEMENTS — un debordement n'y est qu'un compte
    suivi actif. Il faut ETALER, pas jeter.
Donc : on trie par montant, on publie les WHALE_MAX plus gros, ON LES MEMORISE,
et les autres restent candidats au tour suivant. Rien n'est enterre.

Construit OFF par defaut (`WHALE_ON`) : on calibre en SIMULER avant d'allumer.

═══════════════════════════════════════════════════════════════════════════
⭐⭐ 27/07/2026 — « JE N'AI D'ALERTE QUE POUR UN SEUL COMPTE SUIVI » (Preda)
═══════════════════════════════════════════════════════════════════════════
Il avait raison, et le defaut etait ENTIEREMENT dans l'identification.

CE QUI A ETE MESURE, pas suppose :
  · le pont exportait 7 comptes tagues, dont **5 sans aucun wallet** ;
  · `_tracke()` ne savait rapprocher que par WALLET ou par PSEUDO ;
  · sonde du 27/07 sur **14 000 transactions VeVe reelles** (3 jours) :
    Omegatron88, RaVeN100 et SwampyNumber5 y sont bien presents — mais
    `buyer_username` / `seller_username` valent **null**. Leur seule identite
    dans ce flux est `buyer_id` / `seller_id` = le **veve_user_id** ;
  · aucun des 7 comptes n'apparait dans le flux StackR sur la meme fenetre.
    Or ce module ne lisait QUE StackR.

⭐ DEUX ANGLES MORTS SUPERPOSES, et aucun des deux ne produisait d'erreur :
   1. LA CLE : la seule identite disponible (veve_user_id) n'etait ni
      exportee par le pont, ni lue ici ;
   2. LE MARCHE : les comptes suivis achetent/vendent sur **VeVe**, pas sur
      StackR. On les cherchait au mauvais endroit.

CE QUI CHANGE :
  a. `charger_tracked` indexe AUSSI par veve_user_id (3e index) ;
  b. `detect_veve` lit le flux `getVeveTransactions` — celui que floor_watch
     pagine DEJA chaque heure pour l'historique des ventes : **zero requete
     de plus**, on branche un rappel sur des pages deja demandees ;
  c. MOISSON DE WALLET : quand un veve_user_id suivi est vu avec une adresse,
     on la memorise dans l'etat (`whale_wallets`) et on la rend a l'index —
     les gros transferts on-chain deviennent possibles pour ce compte des le
     lendemain, sans cookie StackR et sans intervention.
  d. le module DIT, a chaque run, quels comptes n'ont jamais rien matche et
     POURQUOI (`journal_identite`). Un silence explique n'est plus un silence.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

from scraper import floor_watch as fw

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WHALE_ON = os.environ.get("WHALE_ON", "false").lower() == "true"
# Canal DEDIE. A defaut de secret, on retombe sur le webhook principal (rien
# perdu, mais tout arrive dans le meme salon).
WEBHOOK = (os.environ.get("DISCORD_WEBHOOK_WHALE", "").strip()
           or os.environ.get("DISCORD_WEBHOOK", "").strip())
TRACKED_CSV = os.environ.get("TRACKED_CSV", "_preda/data/tracked_accounts.csv")
STATE_PATH = os.environ.get("WHALE_STATE", "data/whale_state.json")

POLLS = int(os.environ.get("WHALE_POLLS", "25"))
INTERVAL_S = float(os.environ.get("WHALE_INTERVAL_S", "120"))
REFRESH_MIN = float(os.environ.get("WHALE_REFRESH_MIN", "60"))  # transferts 1x/h
MIN_USD = float(os.environ.get("WHALE_MIN_USD", "1"))     # ignore la poussiere
XFER_MIN = int(os.environ.get("WHALE_XFER_MIN", "5"))     # « gros » = >= N jetons/tx
MAX_CARTES = int(os.environ.get("WHALE_MAX", "10"))
VU_TTL = float(os.environ.get("WHALE_VU_TTL_H", "48")) * 3600
XFER_PAGES = int(os.environ.get("WHALE_XFER_PAGES", "3"))
# ═══════════════════════════════════════════════════════════════════════════
# ⏱️ FRAICHEUR DU FLUX VEVE — pose AVANT que le probleme n'arrive (27/07)
# ═══════════════════════════════════════════════════════════════════════════
# `detect_veve` lit les pages que floor_watch pagine deja pour l'historique des
# ventes. Sa profondeur depend donc de FLOOR_SALES_PAGES, QUI N'EST PAS A MOI :
#     20 pages  =  2 000 tx  ≈ 10 heures   (le reglage actuel de Preda)
#    120 pages  = 12 000 tx  ≈  3 jours    (le defaut du code, qu'il va poser)
#
# ⚠️ LE JOUR OU CE REGLAGE PASSE DE 20 A 120, la fenetre est multipliee par ~7
# D'UN COUP. Tous les evenements de comptes suivis des 3 derniers jours n'ont
# JAMAIS ete vus (ils etaient hors fenetre) : ils apparaitraient donc comme
# neufs et partiraient sur Discord — jusqu'a 10 cartes par tour, pendant
# plusieurs tours, pour des achats vieux de trois jours.
# ⭐ Un elargissement de fenetre ressemble a une rafale d'actualite. Ce n'en
# est pas une : c'est du rattrapage, et personne ne l'aurait demande.
#
# Et editorialement c'est la meme reponse : « un compte suivi VIENT d'acheter »
# n'a de sens que si c'est recent. A trois jours, ce n'est plus une alerte.
# On borne donc l'age des evenements publies par ce detecteur. Le run tourne au
# pire 1x/h : 6 h de fenetre couvrent largement, meme apres plusieurs crons
# sautes (ecart de refresh mesure le 22/07 : 233 min).
# 0 = pas de borne (l'ancien comportement, si Preda veut tout rattraper).
VEVE_MAX_AGE_H = float(os.environ.get("WHALE_VEVE_MAX_AGE_H", "6"))
SIMULER = os.environ.get("WHALE_SIMULER", "").strip().lower() in ("1", "oui", "true")

# CollectChain (collectscan) — transferts NFT (ERC-721, verifie le 17/07).
API_BASE = "https://collectscan.com/api/v2"
ZERO = "0x0000000000000000000000000000000000000000"
MARKET_ESCROW = "0xb1af72a77b9065c55cda0680b86655a79b62e42c"
BURN_SINK = "0x39e3816a8c549ec22cd1a34a8cf7034b3941d8b1"
SYSTEM = {ZERO, MARKET_ESCROW, BURN_SINK}
UA = {"User-Agent": "veve-whale-watch/1.0", "Accept": "application/json"}

COULEURS = {"achat": 0x2ECC71, "vente": 0xE67E22, "mise en vente": 0xF1C40F,
            "gros transfert": 0x9B59B6}


def _norm(w) -> str:
    return (w or "").strip().lower()


# ---------------------------------------------------------------------------
# Le pont : les comptes suivis (CSV exporte par preda)
# ---------------------------------------------------------------------------

def charger_tracked(chemin: str = None) -> Tuple[Dict[str, Dict],
                                                 Dict[str, Dict],
                                                 Dict[str, Dict]]:
    """Lit les lignes 🟣C-PSEUDOS taguees. Renvoie TROIS index vers la meme
    fiche : par wallet (imx ET stackr), par username, et par veve_user_id.

    ⭐ LE 3e INDEX EST LE CORRECTIF DU 27/07. Dans le flux VeVe, les comptes
    suivis apparaissent avec un pseudo NULL et un `buyer_id`/`seller_id` —
    c'est-a-dire le veve_user_id. Sans cet index, 5 comptes sur 7 etaient
    invisibles sans qu'aucune erreur ne le signale.

    Une ligne SANS « type » est ignoree (seul le tag fait suivre)."""
    chemin = chemin or TRACKED_CSV
    par_wallet: Dict[str, Dict] = {}
    par_user: Dict[str, Dict] = {}
    par_uid: Dict[str, Dict] = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                typ = (r.get("type") or r.get("Type de compte") or "").strip()
                if not typ:
                    continue
                user = (r.get("username") or "").strip()
                uid = (r.get("veve_user_id") or "").strip()
                fiche = {
                    "username": user or "(sans pseudo)",
                    "type": typ,
                    "holdings": (r.get("holdings") or "").strip(),
                    "value_floor": (r.get("value_floor") or "").strip(),
                    "wallet_imx": _norm(r.get("wallet_imx")),
                    "wallet_stackr": _norm(r.get("wallet_stackr")),
                    "veve_user_id": uid,
                }
                for w in (fiche["wallet_imx"], fiche["wallet_stackr"]):
                    if w:
                        par_wallet[w] = fiche
                if user:
                    par_user[user.lower()] = fiche
                if uid:
                    par_uid[uid] = fiche
    except FileNotFoundError:
        print(f"  (pas de {chemin} : aucun compte suivi — preda ne l'a pas "
              f"encore exporte)", file=sys.stderr)
    return par_wallet, par_user, par_uid


def _tracke(tracked, wallet, username, uid=None) -> Optional[Dict]:
    """⚠️ TOLERANT A DEUX FORMES de `tracked` : (wallet, user) — l'ancienne,
    encore utilisee par les tests — et (wallet, user, uid) — la nouvelle. Un
    correctif qui casse la suite de tests ne se deploie jamais."""
    par_wallet, par_user = tracked[0], tracked[1]
    par_uid = tracked[2] if len(tracked) > 2 else {}
    return (par_wallet.get(_norm(wallet))
            or par_user.get((username or "").strip().lower())
            or (par_uid.get(str(uid).strip()) if uid else None))


# ---------------------------------------------------------------------------
# 🧭 MOISSON DE WALLET — un compte suivi finit toujours par se montrer
# ---------------------------------------------------------------------------

def apprendre_wallet(state, tracked, fiche, wallet) -> bool:
    """Un compte suivi vient d'etre reconnu (par son veve_user_id) EN FACE
    d'une adresse : on l'apprend une fois pour toutes.

    POURQUOI CA COMPTE : sans wallet, `detect_transferts` ne peut rien voir
    pour ce compte — l'explorateur on-chain s'interroge PAR ADRESSE. Resoudre
    pseudo -> wallet cote StackR demande un cookie verifiedVeve perissable
    (`pseudos-stackr`, hebdo, fast-skip quand le cookie est mort — il l'etait).
    Ici c'est gratuit, sans cookie, et ca se repare tout seul.

    L'adresse est ecrite dans l'etat, pas dans le Sheet : ce module ne decide
    pas a la place de Preda. Le log lui dit quoi coller dans 🟣C-PSEUDOS."""
    w = _norm(wallet)
    if not w or w in SYSTEM:
        return False
    connus = state.setdefault("whale_wallets", {})
    uid = fiche.get("veve_user_id") or fiche.get("username") or ""
    if connus.get(uid) == w:
        return False
    if fiche.get("wallet_imx") == w or fiche.get("wallet_stackr") == w:
        return False
    connus[uid] = w
    if not fiche.get("wallet_imx"):
        fiche["wallet_imx"] = w
    tracked[0][w] = fiche             # actif des ce run pour les transferts
    print(f"  🧭 wallet appris : {fiche['username']} -> {w}  "
          f"(a coller dans 🟣C-PSEUDOS, colonne wallet_imx — les gros "
          f"transferts on-chain de ce compte deviennent visibles)", flush=True)
    return True


def restaurer_wallets(state, tracked) -> int:
    """Reinjecte les wallets appris aux runs precedents. Sans ca, la moisson
    serait a refaire a chaque run (l'etat sert a QUELQUE CHOSE)."""
    connus = state.get("whale_wallets") or {}
    if not connus:
        return 0
    par_uid = tracked[2] if len(tracked) > 2 else {}
    n = 0
    for uid, w in connus.items():
        fiche = par_uid.get(uid)
        if not fiche:
            fiche = next((f for f in tracked[1].values()
                          if f.get("username") == uid), None)
        w = _norm(w)
        if not (fiche and w) or w in tracked[0]:
            continue
        if not fiche.get("wallet_imx"):
            fiche["wallet_imx"] = w
        tracked[0][w] = fiche
        n += 1
    if n:
        print(f"  🧭 {n} wallet(s) appris precedemment, reinjecte(s).",
              flush=True)
    return n


# ---------------------------------------------------------------------------
# Debordement : on etale, on n'enterre pas
# ---------------------------------------------------------------------------

def _rendre(cand, vus, ts, cle_id, poids, quoi):
    """Applique le plafond MAX_CARTES en RENDANT le surplus au tour suivant.

    Contrat, et c'est tout le correctif du 20/07 :
      · ce qui est PUBLIE est MEMORISE (donc ne revient pas) ;
      · ce qui n'est pas publie n'est PAS memorise (donc revient) ;
      · on garde les plus gros d'abord, et on le DIT sur stderr.

    L'ancien code faisait `return []` sans rien memoriser : le surplus revenait,
    mais le lot restait au-dessus du plafond, donc etait rejete a nouveau. Un
    verrou qui ne s'ouvre jamais. Ici le stock devient un debit.

    `poids` : fonction de tri (montant en $, nombre de jetons…), decroissant.
    `cle_id` : nom de la cle de dedoublonnage dans chaque candidat.
    """
    if len(cand) <= MAX_CARTES:
        for c in cand:
            vus[c[cle_id]] = ts
        return cand

    cand = sorted(cand, key=poids, reverse=True)
    publies, rendus = cand[:MAX_CARTES], cand[MAX_CARTES:]
    for c in publies:                      # ⭐ MEMORISER ce qu'on publie
        vus[c[cle_id]] = ts
    print(f"  🔇 🐋 {len(cand)} {quoi} d'un coup — on publie les "
          f"{len(publies)} plus gros, {len(rendus)} rendus au tour suivant.",
          file=sys.stderr)
    return publies


# ---------------------------------------------------------------------------
# 🛒 / 💸 / 🏷️  Evenements de marche (flux StackR, 2 min)
# ---------------------------------------------------------------------------

def detect_marche(state, listings, ventes, tracked, omi, veve=None, cat=None):
    """Achats, ventes et mises en vente d'un compte suivi. Chaque evenement est
    identifie par (genre, nft_id, timestamp) et dedoublonne dans `vus`.

    22/07/2026 — deux manques signales par Preda sur les cartes de prod :
      · PAS de floor actuel ni de plus-bas historique sur la carte (« Zero
        Ghost #1316 · 1,78 $ » tout nu : impossible de juger si c'est cher) ;
      · des evenements publies 2 h 30 apres les faits SANS que rien ne le dise
        (mesure : listes 10:00-10:08 UTC, publies 12:34-12:36 — des runs cron
        sautes par GitHub, puis rattrapes via la fenetre du flux).
    D'ou : `veve` (floors) + `cat` (catalogue) optionnels — memes donnees que
    la boucle floor_watch, ZERO requete en plus — et l'horodatage de
    l'evenement porte sur chaque carte + filtre fw.trop_vieux optionnel."""
    vus = state.setdefault("whale_vus", {})
    ts = time.time()
    veve = veve or {}
    cat = cat or {}
    cand: List[Dict] = []
    local = set()

    def _try(genre, emoji, it, fiche, prix_omi):
        nft = str(it.get("nft_id") or "")
        stamp = str(it.get("timestamp") or "")
        cle = genre + "|" + nft + "|" + stamp
        if not nft or cle in vus or cle in local:
            return
        quand = fw._event_epoch(it)
        if fw.trop_vieux(quand, ts):
            return
        usd = fw._f(prix_omi) * omi if omi else 0.0
        if usd and usd < MIN_USD:
            return
        local.add(cle)
        uid = str(it.get("element_id") or "")
        genre_cat = ("comic" if str(it.get("element_type") or "") == "COMIC_COVER"
                     else "collectible")
        fiche_cat = cat.get(uid) or {}
        floor_veve = fw._f(veve.get(uid))
        last = (state.get("sales") or {}).get(uid)
        cand.append({"cle": cle, "genre": genre, "emoji": emoji,
                     "marche": "StackR",
                     "compte": fiche["username"], "type": fiche["type"],
                     "uuid": uid, "categorie": genre_cat,
                     "name": it.get("name") or fiche_cat.get("name") or uid[:8],
                     "edition": it.get("edition") or "",
                     "usd": round(usd, 2), "omi": round(fw._f(prix_omi)),
                     "quand": quand,
                     # contexte de l'item — ce qui manquait aux cartes :
                     "floor_veve": round(floor_veve, 2) if floor_veve > 0 else None,
                     "atl": fw.atl_connu(state, uid, fiche_cat.get("atl")),
                     "last": (lambda v: round(v, 2) if v > 0 else None)(
                         fw._f((last or [0])[0]))})

    for it in listings or []:
        f = _tracke(tracked, it.get("listed_by"), it.get("listed_by_username"))
        if f:
            _try("mise en vente", "🏷️", it, f, it.get("price"))
    for it in ventes or []:
        fa = _tracke(tracked, it.get("buyer"), it.get("buyer_username"))
        if fa:
            _try("achat", "🛒", it, fa, it.get("price"))
        fv = _tracke(tracked, it.get("listed_by"), it.get("listed_by_username"))
        if fv:
            _try("vente", "💸", it, fv, it.get("price"))

    for k in [k for k, t in list(vus.items()) if ts - fw._f(t) > VU_TTL]:
        vus.pop(k, None)

    # Le plus gros d'abord : si un compte suivi s'agite, on veut ses grosses
    # operations, pas les dix premieres du hasard de l'ordre du flux.
    return _rendre(cand, vus, ts, "cle", lambda c: fw._f(c.get("usd")),
                   "evenements comptes suivis")


# ---------------------------------------------------------------------------
# 🛒 / 💸  Evenements du marche VEVE (flux getVeveTransactions, 1x/h)
# ---------------------------------------------------------------------------

def detect_veve(state, txs, tracked, veve=None, cat=None, ts=None):
    """Achats et ventes d'un compte suivi SUR LE MARCHE VEVE.

    ⭐ POURQUOI CE DETECTEUR EXISTE (27/07). `detect_marche` ne lit que le flux
    StackR. Sonde du 27/07 : sur 14 000 transactions VeVe des 3 derniers jours,
    trois comptes suivis apparaissent — et AUCUN des 7 n'apparait cote StackR
    sur la meme fenetre. On cherchait au mauvais endroit, et un canal qui
    cherche au mauvais endroit ressemble trait pour trait a un canal calme.

    ⚠️ IDENTIFICATION PAR `buyer_id`/`seller_id` (= veve_user_id) EN PREMIER :
    dans ce flux, le pseudo de ces comptes est **null**. C'est la seule cle qui
    marche. L'adresse vue en face est apprise au passage (`apprendre_wallet`).

    ⚠️ UNITE : `price` est DEJA en dollars dans getVeveTransactions (contrairement
    au flux StackR, en OMI). Aucune conversion — la confondre a deja coute cher
    (leçon des unites, v18). Pas d'`omi` en parametre, volontairement.

    ZERO REQUETE : `txs` sont les pages que floor_watch pagine deja chaque heure
    pour l'historique des ventes."""
    ts = ts if ts is not None else time.time()
    vus = state.setdefault("whale_vus", {})
    veve = veve or {}
    cat = cat or {}
    cand: List[Dict] = []
    local = set()
    vieux = [0]          # compte des evenements ecartes pour cause d'age

    for it in txs or []:
        if str(it.get("status") or "") != "COMPLETE":
            continue
        veve_id = str(it.get("veve_id") or "")
        for role, emoji, genre, cle_id, cle_user, cle_addr in (
                ("buyer", "🛒", "achat", "buyer_id", "buyer_username",
                 "buyer_address"),
                ("seller", "💸", "vente", "seller_id", "seller_username",
                 "seller_address")):
            fiche = _tracke(tracked, it.get(cle_addr), it.get(cle_user),
                            it.get(cle_id))
            if not fiche:
                continue
            apprendre_wallet(state, tracked, fiche, it.get(cle_addr))
            cle = "veve|" + genre + "|" + (veve_id or str(it.get("nft_id") or ""))
            if not veve_id or cle in vus or cle in local:
                continue
            usd = fw._f(it.get("price"))          # DEJA en dollars
            if usd and usd < MIN_USD:
                continue
            if usd > fw.PRIX_MAX:
                continue                          # plafond de vraisemblance
            quand = _epoch_veve(it.get("created_at"))
            if fw.trop_vieux(quand, ts):
                continue
            # ⏱️ borne propre a ce detecteur (voir VEVE_MAX_AGE_H) : la
            # profondeur du flux depend de FLOOR_SALES_PAGES, pas de nous.
            if VEVE_MAX_AGE_H > 0 and quand is not None \
                    and (ts - quand) > VEVE_MAX_AGE_H * 3600:
                vieux[0] += 1
                continue
            local.add(cle)
            uid = str(it.get("element_id") or "")
            fiche_cat = cat.get(uid) or {}
            floor_veve = fw._f(veve.get(uid))
            last = (state.get("sales") or {}).get(uid)
            cand.append({
                "cle": cle, "genre": genre, "emoji": emoji, "marche": "VeVe",
                "compte": fiche["username"], "type": fiche["type"],
                "uuid": uid,
                "categorie": ("comic" if str(it.get("element_type") or "")
                              == "COMIC_COVER" else "collectible"),
                "name": it.get("name") or fiche_cat.get("name") or uid[:8],
                "edition": it.get("nft_issue") or "",
                "usd": round(usd, 2), "omi": 0, "quand": quand,
                "floor_veve": round(floor_veve, 2) if floor_veve > 0 else None,
                "atl": fw.atl_connu(state, uid, fiche_cat.get("atl")),
                "last": (lambda v: round(v, 2) if v > 0 else None)(
                    fw._f((last or [0])[0]))})

    for k in [k for k, t in list(vus.items()) if ts - fw._f(t) > VU_TTL]:
        vus.pop(k, None)
    if vieux[0]:
        # ⭐ On le DIT : sans cette ligne, un elargissement de FLOOR_SALES_PAGES
        # donnerait l'impression que le detecteur rate des evenements, alors
        # qu'il les ecarte volontairement parce qu'ils sont vieux.
        print(f"  🐋 {vieux[0]} evenement(s) VeVe ecarte(s) : plus de "
              f"{VEVE_MAX_AGE_H:g} h (rattrapage, pas actualite — "
              f"WHALE_VEVE_MAX_AGE_H=0 pour tout publier).", flush=True)
    return _rendre(cand, vus, ts, "cle", lambda c: fw._f(c.get("usd")),
                   "evenements VeVe comptes suivis")


def _epoch_veve(brut):
    """'2026-07-26T23:33:40.677Z' -> epoch, ou None. On ne devine jamais."""
    brut = str(brut or "").strip()
    if not brut:
        return None
    try:
        d = _dt.datetime.fromisoformat(brut.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 🧾 LE JOURNAL D'IDENTITE — pourquoi un compte suivi ne dit jamais rien
# ---------------------------------------------------------------------------

def journal_identite(state, tracked) -> None:
    """A la fin d'un run : qui, parmi les comptes suivis, n'a JAMAIS rien
    matche, et pour quelle raison verifiable.

    ⭐ C'est la parade au defaut de fond : un canal muet ne disait pas s'il
    etait muet parce que le marche dormait ou parce que 5 comptes sur 7
    n'avaient aucune cle exploitable. Deux causes, un seul symptome — et deux
    remedes opposes. On les separe."""
    fiches = {id(f): f for f in
              list(tracked[0].values()) + list(tracked[1].values())
              + list((tracked[2] if len(tracked) > 2 else {}).values())}
    if not fiches:
        return
    vus_comptes = state.setdefault("whale_comptes_vus", {})
    jamais, sans_cle, sans_wallet = [], [], []
    for f in fiches.values():
        nom = f.get("username") or "(sans pseudo)"
        a_wallet = bool(f.get("wallet_imx") or f.get("wallet_stackr"))
        a_cle = a_wallet or bool(f.get("veve_user_id"))
        if not a_cle:
            sans_cle.append(nom)
        elif not a_wallet:
            sans_wallet.append(nom)
        if nom not in vus_comptes:
            jamais.append(nom)
    print(f"  🧾 comptes suivis : {len(fiches)} · "
          f"{len(fiches) - len(jamais)} ont deja declenche au moins une fois.",
          flush=True)
    if sans_cle:
        print(f"     ⛔ INSUIVABLES (ni wallet ni veve_user_id) : "
              + ", ".join(sorted(sans_cle)[:8])
              + " — completer 🟣C-PSEUDOS, sinon ils ne diront JAMAIS rien.",
              file=sys.stderr)
    if sans_wallet:
        print(f"     ⚠️ sans wallet : " + ", ".join(sorted(sans_wallet)[:8])
              + " — achats/ventes visibles (via veve_user_id), gros transferts "
                "on-chain NON. Le wallet s'apprendra a leur 1re transaction.",
              flush=True)
    if jamais:
        print(f"     💤 aucun evenement vu a ce jour : "
              + ", ".join(sorted(jamais)[:8]), flush=True)


def noter_compte_vu(state, cartes) -> None:
    """Memorise qu'un compte a declenche — c'est ce qui permet au journal de
    distinguer « jamais rien matche » de « calme en ce moment »."""
    vus = state.setdefault("whale_comptes_vus", {})
    for a in cartes or []:
        if a.get("compte"):
            vus[a["compte"]] = time.time()


# ---------------------------------------------------------------------------
# 🔀  Gros transferts NFT hors marche (collectscan, 1x/h)
# ---------------------------------------------------------------------------

def _get(url, params=None):
    for attempt in range(1, 6):
        try:
            r = requests.get(url, params=params or {}, headers=UA, timeout=40)
            if r.status_code == 429:
                time.sleep(3 * attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            if attempt == 5:
                print(f"    collectscan abandonne : {e}", file=sys.stderr)
                return None
            time.sleep(3 * attempt)
    return None


def detect_transferts(state, tracked):
    """Un mouvement NFT wallet-a-wallet d'un compte suivi, >= WHALE_XFER_MIN
    jetons dans une meme transaction. On exclut l'escrow (marche), le zero
    (mint) et le burn sink : ne restent que les vrais transferts. Dedup par tx.
    """
    # ⛔ 27/07 — CE DECOMPACTAGE A FAIT TOMBER LE RUN. `charger_tracked` est
    # passe de 2 index a 3 (ajout de `par_uid`) ; `_tracke` avait ete rendu
    # tolerant, celui-ci avait ete OUBLIE : `par_wallet, _ = tracked` sur un
    # triplet leve `ValueError: too many values to unpack`.
    # ⭐ LA LEÇON : quand on elargit une structure partagee, ce n'est pas la
    # fonction qu'on modifie qu'il faut relire — c'est TOUS SES LECTEURS.
    # `grep "= tracked"` aurait suffi, et c'est ce que fait desormais
    # `test_aucun_lecteur_ne_decompacte_tracked_en_deux`.
    par_wallet = tracked[0]
    vus = state.setdefault("whale_tx_vus", {})
    ts = time.time()
    cand: List[Dict] = []

    for wallet, fiche in par_wallet.items():
        if not wallet:
            continue
        url = f"{API_BASE}/addresses/{wallet}/token-transfers"
        params = {"type": "ERC-721"}
        groups: Dict[str, Dict] = {}
        pages = 0
        while pages < XFER_PAGES:
            d = _get(url, params)
            if not d:
                break
            for it in d.get("items") or []:
                tot = it.get("total") or {}
                inst = (tot.get("token_instance") or {}) if isinstance(tot, dict) else {}
                if not inst:
                    continue                       # pas un NFT VeVe (pas d'instance)
                frm = _norm((it.get("from") or {}).get("hash"))
                to = _norm((it.get("to") or {}).get("hash"))
                if wallet not in (frm, to):
                    continue
                autre = to if frm == wallet else frm
                if autre in SYSTEM:                # marche / mint / burn : ecarte
                    continue
                txh = it.get("transaction_hash") or it.get("tx_hash") or ""
                if not txh:
                    continue
                md = inst.get("metadata") or {}
                g = groups.setdefault(txh, {
                    "count": 0,
                    "sens": "sortant" if frm == wallet else "entrant",
                    "autre": autre, "name": (md.get("name") if isinstance(md, dict) else "") or "",
                    "ts": it.get("timestamp") or ""})
                g["count"] += 1
            nxt = d.get("next_page_params")
            if not nxt:
                break
            params = {"type": "ERC-721", **nxt}
            pages += 1
            time.sleep(0.15)

        for txh, g in groups.items():
            if g["count"] < XFER_MIN or txh in vus:
                continue
            cand.append({"genre": "gros transfert", "emoji": "🔀",
                         "compte": fiche["username"], "type": fiche["type"],
                         "txh": txh, "count": g["count"], "sens": g["sens"],
                         "autre": g["autre"], "name": g["name"] or "?"})

    for k in [k for k, t in list(vus.items()) if ts - fw._f(t) > VU_TTL]:
        vus.pop(k, None)

    # Ici le « poids » est le nombre de jetons deplaces dans la transaction.
    return _rendre(cand, vus, ts, "txh", lambda c: fw._f(c.get("count")),
                   "gros transferts")


# ---------------------------------------------------------------------------
# Cartes + envoi
# ---------------------------------------------------------------------------

def carte(a):
    ou = " sur " + a["marche"] if a.get("marche") else ""
    tete = "{} {}{} — {} ({})".format(a["emoji"], a["genre"].capitalize(), ou,
                                      a["compte"], a["type"])
    if a["genre"] == "gros transfert":
        lien = "https://collectscan.com/tx/" + a["txh"]
        desc = ["**{}** jeton(s) {} · contrepartie `{}`".format(
                    a["count"], a["sens"], a["autre"][:10] + "…"),
                a["name"],
                "[Voir la transaction](" + lien + ")"]
    else:
        # ⭐ 27/07 : le lien mene LA OU L'EVENEMENT A EU LIEU. Un achat conclu
        # sur le marche VeVe pointe vers VeVe — l'envoyer sur StackR serait un
        # lien qui s'ouvre tout en etant faux (piege deja paye sur les crafts).
        sur_veve = a.get("marche") == "VeVe"
        lien = (fw.lien_marche(a["uuid"], a.get("categorie", "")) if sur_veve
                else fw.lien_stackr(a["uuid"], a.get("categorie", "")))
        nom = a["name"] + (" #{}".format(a["edition"]) if a.get("edition") else "")
        if not a["usd"]:
            prix = "prix inconnu"
        elif sur_veve:
            prix = "**{:.2f} $** sur **VeVe**".format(a["usd"])
        else:
            prix = "**{:.2f} $** ({} OMI) sur **StackR**".format(a["usd"],
                                                                a["omi"])
        desc = [nom, prix]
        # Le contexte qui manquait (Preda, 22/07) : sans floor ni plus-bas,
        # « 1,78 $ » ne dit rien. Absent = inconnu, on n'affiche pas de zero.
        if a.get("floor_veve"):
            desc.append("Floor VeVe : **{:.2f} $**".format(a["floor_veve"]))
        if a.get("atl"):
            desc.append(fw.ligne_atl(a["atl"], a.get("usd")))
        if a.get("last"):
            desc.append("Derniere vente reelle : {:.2f} $".format(a["last"]))
        verbe = {"achat": "Acheté", "vente": "Vendu"}.get(a["genre"], "Listé")
        lq = fw.ligne_quand(verbe, a.get("quand"))
        if lq:
            desc.append(lq)
        desc.append("[Voir sur {}]({})".format(
            "VeVe" if sur_veve else "StackR", lien))
    return {"title": tete[:250], "color": COULEURS.get(a["genre"], 0x95A5A6),
            "description": "\n".join(desc),
            "url": None if a["genre"] == "gros transfert" else lien}


def _ligne_sim(a):
    if a["genre"] == "gros transfert":
        return "🔀 {:<20} {} jetons {} <-> {}".format(
            a["compte"][:20], a["count"], a["sens"], a["autre"][:10])
    return "{} {:<20} {:<28} {:>8.2f} $".format(
        a["emoji"], a["compte"][:20], (a["name"][:28]), a["usd"])


def notifier(state, cartes):
    """Un message groupe, 10 cartes max, plafond 20/min par webhook, 429
    respecte — via les garde-fous de floor_watch."""
    if not cartes:
        return 0
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye]", flush=True)
        for a in cartes[:10]:
            print("    " + _ligne_sim(a), flush=True)
        return len(cartes)
    if fw.budget(state, WEBHOOK) <= 0:
        print("  🔇 plafond/min atteint — evenements gardes pour plus tard.",
              flush=True)
        return 0
    contenu = "🐋 **{} evenement(s) — comptes suivis** — {}".format(
        len(cartes), fw.heure_cartes())
    embeds = [carte(a) for a in cartes[:10]]
    # Les evenements de genre « gros transfert » n'ont pas d'uuid : le pont
    # les ignore tout seul (sans identifiant, impossible de dedoublonner ni
    # de croiser une liste de surveillance).
    from scraper import bot_alertes
    bot_alertes.pousser_lot("whale", cartes[:10], embeds, simuler=SIMULER)
    try:
        r = requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        if r.status_code == 429:
            time.sleep(min(fw._f(r.json().get("retry_after")) + 1, 60))
            requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        fw.consommer(state, WEBHOOK)
        print(f"  Discord : {len(embeds)} carte(s) poussee(s).", flush=True)
    except Exception as e:                                      # noqa: BLE001
        print(f"  Discord KO ({e})", flush=True)
    return len(cartes)


# ---------------------------------------------------------------------------
# Etat + main
# ---------------------------------------------------------------------------

def load_state() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                           # noqa: BLE001
        return {}


def save_state(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def main() -> int:
    t0 = time.time()
    state = load_state()
    tracked = charger_tracked()
    n_comptes = len({id(v) for v in tracked[0].values()}
                    | {id(v) for v in tracked[1].values()}
                    | {id(v) for v in tracked[2].values()})
    print("🐋 suivi comptes whale/team : "
          + ("ON" if WHALE_ON else "OFF (WHALE_ON=true pour l'allumer)")
          + (" · SIMULATION" if SIMULER else "")
          + f" · {n_comptes} compte(s) suivi(s) · canal "
          + ("dedie" if os.environ.get("DISCORD_WEBHOOK_WHALE") else "principal"),
          flush=True)
    if not WHALE_ON:
        print("  (le module est eteint — rien n'est fait)", flush=True)
        return 0
    if not tracked[0] and not tracked[1] and not tracked[2]:
        print("  aucun compte suivi : rien a faire.", flush=True)
        return 0
    # 🧭 les wallets appris aux runs precedents redeviennent actifs ici aussi
    # (ce module tourne aussi en autonome, pas seulement pilote par floor_watch).
    restaurer_wallets(state, tracked)

    s = requests.Session()
    dernier_refresh = 0.0
    total = 0
    for i in range(1, POLLS + 1):
        omi = fw.fetch_omi_price(s)
        # transferts on-chain : 1x/h (collectscan est public, on reste poli)
        if time.time() - dernier_refresh > REFRESH_MIN * 60:
            dernier_refresh = time.time()
            xfers = detect_transferts(state, tracked)
            if xfers:
                total += notifier(state, xfers)
            print(f"  transferts on-chain releves ({len(xfers)} gros).",
                  flush=True)
        listings = fw.fetch_listings(s)
        ventes = fw.fetch_sales(s)
        evts = detect_marche(state, listings, ventes, tracked, omi)
        if evts:
            print(f"  [{i}/{POLLS}] 🐋 {len(evts)} evenement(s) marche !",
                  flush=True)
            total += notifier(state, evts)
        else:
            print(f"  [{i}/{POLLS}] {len(listings)} listings, {len(ventes)} "
                  f"vente(s) — rien pour un compte suivi.", flush=True)
        save_state(state)
        if i < POLLS:
            time.sleep(INTERVAL_S)
    print(f"Termine : {POLLS} tours, {total} evenement(s), "
          f"{time.time() - t0:.0f}s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
