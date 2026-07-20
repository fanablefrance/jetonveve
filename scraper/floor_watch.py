# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : scraper/floor_watch.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🚨 ALERTES SOUS-FLOOR v3 — le flux des LISTINGS (12/07/2026).

v1/v2 balayaient les 6 011 floors (61 requetes/tour) en esperant voir un floor
s'effondrer. La capture DevTools de Preda a revele BIEN mieux :

  `publicVeve.getAllLatestListings_v2` — PUBLIC, sans cookie : le flux des
  MISES EN VENTE, et chaque ligne porte deja tout ce qu'il faut :
      price (OMI) · nft_id · element_id (= veve_uuid) · edition · name ·
      rarity · timestamp · listed_by (+ username) · **stackr_floor_price**
  ~695 listings par jour -> 1 requete toutes les 2 minutes SUFFIT (au lieu de
  61 par tour). C'est plus reactif ET beaucoup plus discret.

  ATTENTION : l'input DOIT contenir le bloc `meta` (les champs null sont
  encodes en "undefined" par superjson) — un input allege renvoie du vide.

ATTENTION AU SENS DES COLONNES (correction de Preda, capture a l'appui) :
  `price` = ce que DEMANDE le vendeur de CE listing ;
  `stackr_floor_price` = l'offre la MOINS CHERE du marche StackR pour cet item
  (celle de quelqu'un d'autre). Ex. reel : Starlight Orb liste a 295 000 OMI
  (50,15 $) alors que le floor StackR est a 8 000 OMI (1,36 $).

TROIS SIGNAUX, DEUX MARCHES (tout verifie le 12/07) :
  1. LISTING sous le floor StackR : price < stackr_floor_price (les deux en
     OMI) -> quelqu'un vient de brader.
  2. LISTING sous le floor VeVe : price converti en $ (via getTokenPrices ->
     omiPrice) < floor VeVe (getElements, en gems ~ $).
  3. ECART ENTRE MARCHES (ajoute apres la remarque de Preda) : meme sans
     nouveau listing, une offre DEJA EN PLACE peut etre bien moins chere sur un
     marche que sur l'autre. Ex. reel : Starlight Orb, floor StackR 8 000 OMI =
     1,36 $ contre un floor VeVe de 6,59 $. On compare donc aussi les DEUX
     FLOORS entre eux (le floor StackR est memorise au fil des listings vus).
     C'est l'arbitrage : acheter au floor StackR, revendre au floor VeVe.

Conforme a la regle des collecteurs longs : etat persistant, jamais de perte,
backoff, et auto-controle de la pagination (`page`, pas `cursor` — verifie).

Env : DISCORD_WEBHOOK (sinon simulation dans les logs), FLOOR_DROP_PCT (10),
      FLOOR_POLLS (25), FLOOR_INTERVAL_S (120), FLOOR_LISTINGS (50),
      FLOOR_REFRESH_MIN (60 = rafraichissement des floors VeVe),
      FLOOR_MIN_USD (1 = ignore les broutilles), FLOOR_COOLDOWN_H (6),
      FLOOR_STATE (data/floor_state.json).
"""

from __future__ import annotations

import datetime as _dt
import hashlib as _hashlib
import json
import os
import sys
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

from scraper import numeros as nu
from scraper import price_baseline as pb

# Pont vers le bot d'alertes (etape 4 du bot Discord). Volontairement
# tolerant : si le fichier manque, floor_watch doit continuer a tourner
# comme avant. Un module d'agrement ne rend jamais un collecteur dependant
# de lui.
try:
    from scraper import bot_alertes
except Exception:                                          # noqa: BLE001
    class _BotMuet:
        @staticmethod
        def pousser_lot(*a, **k):
            return 0
    bot_alertes = _BotMuet()

BASE = "https://www.stackr.world/api/trpc/publicVeve."
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

STATE_PATH = os.environ.get("FLOOR_STATE", "data/floor_state.json")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DROP_PCT = float(os.environ.get("FLOOR_DROP_PCT", "10"))
POLLS = int(os.environ.get("FLOOR_POLLS", "25"))
INTERVAL_S = int(os.environ.get("FLOOR_INTERVAL_S", "120"))
N_LISTINGS = int(os.environ.get("FLOOR_LISTINGS", "50"))
REFRESH_MIN = float(os.environ.get("FLOOR_REFRESH_MIN", "60"))
MIN_USD = float(os.environ.get("FLOOR_MIN_USD", "1"))
SPREAD_PCT = float(os.environ.get("FLOOR_SPREAD_PCT", "40"))
# TROIS SEUILS DISTINCTS (1er run reel, 12/07 : 15 alertes sur 50 listings avec
# un seuil unique a 10 % !). La lecture des alertes est sans appel : les offres
# StackR sont STRUCTURELLEMENT moins cheres que le floor VeVe (marche moins
# liquide, prix en OMI). Comparer un prix StackR au floor VeVe a 10 % revient
# donc a alerter en permanence. On exige beaucoup plus pour cette comparaison
# inter-marches, et on garde un seuil bas pour la seule qui soit vraiment
# "toutes choses egales par ailleurs" : prix StackR vs floor StackR.
VEVE_PCT = float(os.environ.get("FLOOR_VEVE_PCT", "35"))
# ARBITRAGE VERIFIE A LA MAIN PAR PREDA (12/07) : Donatello - IncogNinja, offre
# StackR a 21,72 $, floor VeVe reellement a 30,00 $ (verifie dans l'app). Le
# signal est donc REEL. Mais une decote brute ne dit pas si l'affaire est
# bonne : ce qui compte, c'est ce qui RESTE APRES LES FRAIS. On raisonne donc
# en MARGE NETTE :
#     benefice = floor_VeVe x (1 - frais) - prix_d_achat
# Frais VeVe ~8,5 % (2,5 % VeVe + fee licensor — cf. marketFee du catalogue).
# NUANCE HONNETE : le floor VeVe est un prix DEMANDE, pas un acheteur qui
# attend. Revendre suppose de se placer sous ce floor et d'attendre preneur —
# la marge affichee est un PLAFOND, pas un gain garanti.
FEE_PCT = float(os.environ.get("FLOOR_FEE_PCT", "8.5"))
MARGIN_PCT = float(os.environ.get("FLOOR_MARGIN_PCT", "20"))
MIN_PROFIT = float(os.environ.get("FLOOR_MIN_PROFIT", "5"))
# HISTORIQUE DES VENTES (1er run reel : TOUTES les alertes disaient « aucune
# vente recente vue » — getAllLatestSales_v2 ne montre que ~50 ventes du jour).
# On reconstruit donc la derniere vente de chaque element depuis le flux COMPLET
# getVeveTransactions (celui du backfill : il pagine loin, 100 tx/page).
# FLOOR_SALES_PAGES=120 -> ~12 000 tx -> ~7 jours de ventes, 1 fois par run.
SALES_PAGES = int(os.environ.get("FLOOR_SALES_PAGES", "120"))
SALES_TYPES = ("MARKET_FIXED", "MARKET_AUCTION", "MARKET_STACKR")
# Un item SANS AUCUNE VENTE dans cette fenetre est illiquide : « revendre au
# floor VeVe » y est une FICTION. Par defaut on n'alerte donc pas sur l'ecart
# entre marches sans preuve de vente (FLOOR_REQUIRE_SALE=false pour desactiver).
REQUIRE_SALE = os.environ.get("FLOOR_REQUIRE_SALE", "true").lower() != "false"

# ⚠️ MODE REGLAGE. Desserrer un seuil et decouvrir le resultat SUR LE DISCORD DE
# LA COMMU, c'est se tromper devant tout le monde. FLOOR_SIMULER=1 : on calcule
# tout, on n'envoie RIEN, on ecrit dans les logs.
SIMULER = os.environ.get("FLOOR_SIMULER", "").lower() in ("1", "oui", "true")
# L'etat contient des ventes collectees AVEC LA MAUVAISE UNITE (divisees par
# ~5 900). Elles ne se corrigent pas toutes seules : l'historique ne re-ecrit pas
# une cle deja connue. Il faut les JETER une fois — sinon elles continueraient
# d'ecraser les marges et de faire taire les alertes.
PURGER_VENTES = os.environ.get("FLOOR_PURGER_VENTES", "").lower() in (
    "1", "oui", "true")

# ═══════════════════════════════════════════════════════════════════════════
# 📚 SIGNAL 4 — LE COMIC A PETIT TIRAGE, BRADE (demande de Preda, 14/07)
# ═══════════════════════════════════════════════════════════════════════════
# « Un comic a 1 000 exemplaires liste sous 2 $, sur VeVe ou sur StackR. »
#
# Ce n'est PAS un arbitrage, et c'est ce qui change tout :
#   * on ne compare rien, on ne promet aucune revente : on constate un PRIX
#     D'ENTREE absurde au regard de la rarete ;
#   * donc **pas de preuve de vente exigee**. Le garde-fou « 71 % des items ne
#     se vendent jamais » protege les promesses de PLUS-VALUE. Ici il n'y en a
#     pas : a 2 $ pour 1/1000, le risque est de 2 $. Exiger une vente recente
#     ferait taire exactement les items les plus dormants — c'est-a-dire
#     precisement ceux qu'on cherche.
#
# Le TIRAGE ne vient pas de StackR (son champ `quantity` n'est pas prouve) mais
# du CSV exporte par preda depuis le Sheet (`export_elements.py`), recupere ici
# en lecture seule. ⚠️ Le supply d'un comic est celui de la SERIE, pas la somme de
# ses raretes (piege paye le 14/07 sur Captain America #7).
COMIC_SUPPLY_MAX = int(os.environ.get("COMIC_SUPPLY_MAX", "1000"))
COMIC_MAX_USD = float(os.environ.get("COMIC_MAX_USD", "2"))
# GARDE-FOU ANTI-DELUGE : si le 1er passage trouve des dizaines de comics sous
# le seuil, c'est le SEUIL qui est mal regle — on ne noie pas la commu sous
# 200 cartes. On ne publie RIEN, on ne memorise RIEN (sinon on les enterrerait
# pour de bon), on CRIE, et l'humain tranche.
COMIC_MAX_ALERTES = int(os.environ.get("COMIC_MAX_ALERTES", "25"))
# ⚠️ LA REGLE DE LA PROFONDEUR DU CARNET (Preda, 14/07) : « Spider-Man #546 a
# 1 000 de supply, mais il y a HUIT offres a 1,75 $ — ce n'est pas une bonne
# affaire. » Il a raison, et c'est general : **un prix bas sur une offre UNIQUE
# est un signal ; le meme prix sur huit offres est un PLAFOND.** Sans la
# profondeur du carnet, on confond la rarete et le prix du marche.
# La donnee vient du CSV (market_totalListings, collecte chaque jour par
# comic_prices depuis my-nft-tracker). VIDE = inconnu : on n'invente pas un zero.
COMIC_MAX_LISTINGS = int(os.environ.get("COMIC_MAX_LISTINGS", "3"))
# ⚠️ REGLE DE LA PLATEFORME (Preda, 14/07) : **on ne peut pas lister sous 1 $ sur
# VeVe** ; sur StackR, si. Donc un floor VeVe a 1,00 $ n'est PAS une aubaine :
# c'est le PLANCHER de la plateforme. L'item n'y est pas brade, il est au prix
# minimum autorise — et beaucoup y touchent. Sur StackR, un prix sous 1 $ reste
# une vraie decote.
PLANCHER_VEVE = float(os.environ.get("VEVE_PRIX_PLANCHER", "1"))
# 🚫 PLAFOND DE VRAISEMBLANCE (audit du 19/07/2026, sur l'etat de prod).
# Il y a un plancher depuis toujours, il manquait le plafond. Releve reel :
# 20 items affichent un floor > 100 000 $ (jusqu'a 42 000 MILLIARDS — une
# offre farfelue seule en vitrine), et une vente est enregistree a
# 18 666 667 $, soit ×1 867 son floor.
# Ces valeurs ne font pas de VOLUME (elles sont rares), elles font des
# CARTES INVRAISEMBLABLES. Et une seule carte « vendu 18 666 667 $ »
# decredibilise un canal entier — surtout un canal payant.
# ⚠️ REQUIRE_SALE en ecarte deja 19 sur 20 cote floors ; il n'y a rien
# d'equivalent cote VENTES, ou une seule ligne suffit.
#
# 50 000 $ : valeur choisie par Preda le 20/07, et bien fondee — les grails
# du marche VeVe ne depassent pas ~15 000 $. Un plafond a plus de trois fois
# le plus cher item connu ne peut pas ecarter une vraie affaire ; il n'ecarte
# que des offres farfelues.
PRIX_MAX = float(os.environ.get("FLOOR_PRIX_MAX", "").strip() or 50000)

# 🛡️ SENTINELLE DE RECOLTE MAIGRE (audit du 19/07/2026).
# fetch_veve_floors pagine ~76 pages et SAUTE EN SILENCE celles qui
# echouent. Un blocage partiel remontait donc une carte des floors
# amputee, que `if neuf:` acceptait telle quelle — un dictionnaire d'un
# seul item est « vrai ». Resultat : 📉 🆕 🩸 ne surveillaient plus qu'une
# poignee d'items, l'horloge de fraicheur se declarait a jour, et le
# journal restait vide.
# ⭐ Un blocage ressemblait EXACTEMENT a un marche calme.
# Desormais : sous FLOOR_MIN_RATIO de la meilleure recolte connue, on
# REFUSE la carte et on GARDE l'ancienne. Mieux vaut des floors d'il y a
# une heure que 98 % d'angles morts qu'on ignore.
FLOOR_MIN_RATIO = float(os.environ.get("FLOOR_MIN_RATIO", "0.8"))
# Combien de refus consecutifs avant de crier sur Discord.
FLOOR_MAIGRE_ALERTE = int(os.environ.get("FLOOR_MAIGRE_ALERTE", "3"))

# ═══════════════════════════════════════════════════════════════════════════
# LES INTERRUPTEURS — « les ecarts de prix, pas pour l'instant » (Preda, 14/07)
# ═══════════════════════════════════════════════════════════════════════════
# On n'EFFACE pas ce code : il est juste, il a coute cher (le bug des unites), et
# il se rallume par une variable de depot le jour ou Preda le voudra.
ARBITRAGE_ON = os.environ.get("FLOOR_ARBITRAGE", "false").lower() == "true"
SPREAD_ON = os.environ.get("FLOOR_SPREAD", "false").lower() == "true"

# ═══════════════════════════════════════════════════════════════════════════
# LOT 1 (15/07) — DEUX SIGNAUX SINGLE-MARCHE (sans l'arbitrage inter-marches)
# ═══════════════════════════════════════════════════════════════════════════
# On construit ces deux canaux ETEINTS par defaut : on les allume et on les
# calibre UN A LA FOIS, en simuler d'abord, comme tout le reste.
#
# A. 🩸 LE VOL SUR LE FLOOR VEVE. Le floor VeVe d'un item s'effondre par rapport
#    a sa valeur RECENTE (memorisee au dernier rafraichissement) : quelqu'un
#    vient de brader sur le marche ou l'on ACHETE vraiment. Single-marche (floor
#    VeVe vs floor VeVe) — donc RIEN a voir avec l'arbitrage inter-marches
#    eteint. C'est la finalite d'origine : « une offre placee sous le floor
#    DEVIENT le floor ». Preuve de vente exigee (REQUIRE_SALE) : un floor qui
#    tombe sur un item qui ne se vend jamais n'est pas une affaire. Et on ignore
#    le plancher plateforme (1 $).
#    ⚠️ Le floor VeVe n'est rafraichi qu'1x/h -> ce signal se juge d'un
#    rafraichissement a l'autre (granularite horaire, coherente avec le cron).
STEAL_ON = os.environ.get("VEVE_STEAL_ON", "false").lower() == "true"
STEAL_PCT = float(os.environ.get("VEVE_STEAL_PCT", "25"))
STEAL_MIN_USD = float(os.environ.get("VEVE_STEAL_MIN_USD", str(MIN_USD)))
STEAL_MAX = int(os.environ.get("VEVE_STEAL_MAX", "10"))
# B. 📈 LA VENTE TRES AU-DESSUS DU FLOOR. Un item vient de se VENDRE (vente
#    reelle, pas une offre) pour bien plus que son floor : la demande chauffe.
#    Signal d'INFORMATION, pas une affaire a saisir — donc pas de preuve de
#    vente a exiger (la vente EST la preuve). On compare de preference au floor
#    du MEME marche (StackR, memorise), a defaut au floor VeVe, et toujours en
#    dollars pour ne pas melanger les unites (leçon v18).
SPIKE_ON = os.environ.get("SALE_SPIKE_ON", "false").lower() == "true"
SPIKE_RATIO = float(os.environ.get("SALE_SPIKE_RATIO", "3"))
SPIKE_MIN_USD = float(os.environ.get("SALE_SPIKE_MIN_USD", "5"))
SPIKE_MAX = int(os.environ.get("SALE_SPIKE_MAX", "10"))

# ═══ CANAL DEDIE (15/07) : 🆕 ATH + 📈 (x3) + pic hors drop → webhook separe ═══
# Si le secret n'est pas defini, on retombe sur le webhook principal (rien perdu).
WEBHOOK_ATH = os.environ.get("DISCORD_WEBHOOK_ATH", "").strip() or WEBHOOK
# 🆕 NOUVEAU ATH : un floor qui bat son plus-haut historique (ATH du tracker,
# affine en direct). Signal marche -> canal dedie.
ATH_ON = os.environ.get("ATH_ON", "false").lower() == "true"
ATH_MAX = int(os.environ.get("ATH_MAX", "10"))
# On n'alerte un nouvel ATH que s'il depasse l'ancien d'au moins ce %.
ATH_MARGIN_PCT = float(os.environ.get("ATH_MARGIN_PCT", "25"))
# 📉 PLUS-BAS HISTORIQUE : un floor qui touche son ATL. Signal d'achat -> canal
# principal (avec 🩸/🎯/📚).
ATL_ON = os.environ.get("ATL_ON", "false").lower() == "true"
ATL_MAX = int(os.environ.get("ATL_MAX", "10"))
# ⚠️ On n'alerte un plus-bas que s'il est au moins ce % SOUS l'ATL connu
# (sinon 1,29 -> 1,28 declencherait une notif inutile — demande Preda 15/07).
ATL_MARGIN_PCT = float(os.environ.get("ATL_MARGIN_PCT", "25"))
# ⚠️ Le tracker renvoie parfois un ATH aberrant (1e15 = listing troll/fat-finger).
# Au-dela de ce plafond, l'ATH est juge INCONNU (ni signal, ni affichage).
ATH_CAP = float(os.environ.get("ATH_CAP", "1000000000"))

# 📉 FRAICHEUR DU PLUS-BAS VeVe (demande Preda 17/07) — le floor VeVe n'est
# lisible qu'1x/h (getElements, sans cookie). Un plus-bas forme a 09:20 n'est
# « vu » qu'au prochain refresh reussi : si un refresh a saute, l'ecart s'empile
# a ~2 h et l'alerte n'est plus instantanee. On n'alerte donc un plus-bas VeVe
# QUE si le refresh PRECEDENT date de <= ATL_FRESH_MIN minutes (le plus-bas est
# alors survenu dans la derniere heure) ; sinon rien (la reference reste a jour).
ATL_FRESH_MIN = float(os.environ.get("ATL_FRESH_MIN", "70"))

# 📉 PLUS-BAS StackR FRAIS — la reponse « vraiment instantane » : le flux StackR
# (getAllLatestListings_v2/Sales) est sonde toutes les 2 min. Une mise en vente
# ou une vente a un prix JAMAIS VU aussi bas sur le marche StackR pour cet item
# declenche en <= 2 min. Reference = plus-bas StackR observe EN DIRECT ($, memoire
# separee `atl_stackr`), JAMAIS le floor VeVe : StackR est structurellement moins
# cher (leçon arbitrage), comparer au floor VeVe alerterait en continu. Construit
# OFF par defaut (on calibre en SIMULER avant d'allumer, comme tout signal neuf).
ATL_STACKR_ON = os.environ.get("ATL_STACKR_ON", "false").lower() == "true"
ATL_STACKR_MIN_USD = float(os.environ.get("ATL_STACKR_MIN_USD", "1"))
# Il faut battre l'ancien plus-bas d'au moins ce % (sinon 1,29 -> 1,28 = bruit).
ATL_STACKR_MARGIN_PCT = float(os.environ.get("ATL_STACKR_MARGIN_PCT", "5"))
ATL_STACKR_MAX = int(os.environ.get("ATL_STACKR_MAX", "10"))

# ═══════════════════════════════════════════════════════════════════════════
# 🌉 LE PONT VEILLE → 🟠H-PRIX (15/07)
# ═══════════════════════════════════════════════════════════════════════════
# On observe DEJA le floor VeVe de ~6 000 elements 1x/h (getElements). Au lieu
# de le jeter, on l'ecrit dans un CSV que preda ingere dans 🟠H-PRIX (page
# d'historique append-on-change). Plus frais (horaire) et plus large que le scan
# quotidien de floors.py, ZERO requete en plus.
# ⚠️ UNITE : getElements `floor_market_price` est en gems ~ $, EXACTEMENT la meme
# que `market_lowestOffer` de 🟠H-PRIX (verifie 15/07 : Sea Queen = 5 000 000 des
# deux cotes) -> AUCUNE conversion. Les trolls (>= HPRIX_FEED_CAP) sont ignores
# comme partout ailleurs. COLLECTIBLES uniquement : les comics restent la
# propriete de comic_prices.py (une seule source par champ).
# On n'emet qu'un VRAI changement (vs la derniere valeur emise, `hprix_emitted`).
# OFF par defaut : on l'allume une fois le pont verifie de bout en bout.
HPRIX_FEED_ON = os.environ.get("HPRIX_FEED_ON", "false").lower() == "true"
HPRIX_FEED_PATH = os.environ.get("HPRIX_FEED", "data/hprix_feed.csv")
HPRIX_FEED_DAYS = int(os.environ.get("HPRIX_FEED_DAYS", "7"))
HPRIX_FEED_CAP = float(os.environ.get("HPRIX_FEED_CAP", str(ATH_CAP)))

# 🔥 LOT 3 (15/07) — PIC D'ACTIVITE HORS DROP → canal dedie (WEBHOOK_ATH). Un
# item DEJA installe dont les VENTES du jour explosent vs sa moyenne des jours
# actifs precedents (source : le flux getVeveTransactions, deja pagine par
# fetch_history — zero requete en plus). Le « hors drop » est GRATUIT : un item
# qui vient de dropper n'a pas d'historique -> baseline vide -> ecarte tout seul.
# OFF par defaut (on calibre le seuil avant d'allumer, comme les autres canaux).
PIC_ON = os.environ.get("PIC_ON", "false").lower() == "true"
PIC_RATIO = float(os.environ.get("PIC_RATIO", "3"))         # x la moyenne/jour actif
PIC_MIN_COUNT = int(os.environ.get("PIC_MIN_COUNT", "4"))   # plancher absolu de ventes du jour
PIC_MIN_JOURS = int(os.environ.get("PIC_MIN_JOURS", "2"))   # jours actifs prealables requis
PIC_MIN_BASE = float(os.environ.get("PIC_MIN_BASE", "1"))   # ventes/jour min. AVANT le pic
PIC_MAX = int(os.environ.get("PIC_MAX", "10"))
# 📊🔊 SIGNAUX BASELINES (magasin de prix prices-full) — OFF par defaut.
# 📊 plus-bas de distribution : floor dans les HISTLOW_PCT % les moins chers
# de son histoire multi-annees (canal principal). 🔊 anomalie d'offres : nb
# d'offres >> norme historique (canal dedie). Lisent prices_baselines.csv.gz.
HISTLOW_ON = os.environ.get("HISTLOW_ON", "false").lower() == "true"
HISTLOW_PCT = float(os.environ.get("HISTLOW_PCT", "10"))
HISTLOW_MIN_PTS = int(os.environ.get("HISTLOW_MIN_POINTS", "30"))
VOL_ON = os.environ.get("VOL_ON", "false").lower() == "true"
VOL_RATIO = float(os.environ.get("VOL_RATIO", "3"))

# ═══════════════════════════════════════════════════════════════════════════
# 🎯 SIGNAL 5 — LA CHASSE AUX NUMEROS
# ═══════════════════════════════════════════════════════════════════════════
# Un #1, un 1234, un 2001 sur un Star Wars valent bien plus qu'une edition
# banale. **Mais on n'alerte QUE si le vendeur ne l'a pas vu** : le prix doit
# etre celui d'un numero ORDINAIRE (≤ floor + MINT_MARGE_PCT %). Sinon on
# alerterait sur chaque #1 liste a 50x le floor — du bruit, tous les jours.
#
# ⚠️ Ce signal ne vit que sur le flux StackR : c'est le SEUL qui donne le NUMERO
# D'EDITION de chaque offre. Le marche VeVe ne l'expose plus sans cookie.
MINT_ON = os.environ.get("MINT_ON", "true").lower() != "false"
MINT_MARGE_PCT = float(os.environ.get("MINT_MARGE_PCT", "20"))
MINT_SCORE_MIN = int(os.environ.get("MINT_SCORE_MIN", "2"))
MINT_MAX_ALERTES = int(os.environ.get("MINT_MAX_ALERTES", "25"))
ELEMENTS_CSV = os.environ.get("ELEMENTS_CSV", "_preda/data/elements.csv")


# ═══════════════════════════════════════════════════════════════════════════
# 🔇 LE BUDGET DE MESSAGES — le spam doit etre STRUCTURELLEMENT impossible
# ═══════════════════════════════════════════════════════════════════════════
# Un cooldown par item ne suffit pas : 25 tours par run, 24 runs par jour, et
# chaque tour peut trouver de NOUVEAUX items. Il faut un plafond qui ne depende
# d'aucun reglage fin : au-dela, on se TAIT.
#
# ⚠️ ET SURTOUT : quand le budget est epuise, on NE MEMORISE RIEN. Marquer une
# affaire comme « deja alertee » sans l'avoir envoyee, ce serait l'enterrer pour
# 6 h — le garde-fou detruirait ce qu'il protege (leçon deja payee 2 fois).
# 🔇 PLAFOND ANTI-BAN. Discord tolere ~30 msg/min PAR WEBHOOK ; on se limite a
# MAX_MSG_MIN (20) par webhook sur une fenetre glissante de 60 s. Chaque canal
# (principal / dedie) a son PROPRE compteur — ce sont des buckets Discord
# separes. + 3 s entre deux envois : on espace, on ne mitraille pas, et on
# respecte le 429 dans les notify (retry_after).
MAX_MSG_MIN = int(os.environ.get("FLOOR_MAX_MSG_MIN", "20"))
PAUSE_MSG = float(os.environ.get("FLOOR_PAUSE_MSG_S", "3"))


def _wh_key(wh) -> str:
    # JAMAIS le webhook en clair dans l'etat (repo PUBLIC) : une empreinte.
    return _hashlib.sha1((wh or "-").encode()).hexdigest()[:12]


def budget(state: Dict, wh=None, ts: float = None) -> int:
    """Combien de messages ce WEBHOOK peut encore envoyer dans la minute qui
    vient (fenetre glissante 60 s, plafond MAX_MSG_MIN)."""
    ts = ts if ts is not None else time.time()
    r = state.setdefault("rate", {})
    k = _wh_key(wh)
    recent = [t for t in r.get(k, []) if ts - t < 60]
    r[k] = recent
    return MAX_MSG_MIN - len(recent)


def consommer(state: Dict, wh=None, ts: float = None) -> None:
    ts = ts if ts is not None else time.time()
    r = state.setdefault("rate", {})
    r.setdefault(_wh_key(wh), []).append(round(ts, 1))
    time.sleep(PAUSE_MSG)          # on espace, on ne mitraille pas


def rendre(state: Dict, alerts: List[Dict], comics: bool = False) -> None:
    """Rien n'a ete envoye : on EFFACE les marques, sinon ces affaires seraient
    enterrees pour 6 h sans que personne ne les ait vues."""
    for a in alerts:
        if comics:
            (state.get("comics_vus") or {}).pop(a.get("uuid"), None)
            (state.get("mints_vus") or {}).pop(a.get("nft"), None)
        else:
            (state.get("alerts") or {}).pop(a.get("uuid") or a.get("nft"), None)


def charger_elements(chemin: str = None) -> Dict[str, Dict]:
    """{uuid -> fiche} pour TOUT le catalogue (comics ET collectibles).

    Le pont `export_elements.py` (preda) porte : tirage, 1re edition publique,
    nombre d'offres, note de classement, marque, licence. Rien de tout cela
    n'existe cote StackR — et rien de tout cela ne se devine."""
    import csv as _csv
    chemin = chemin or ELEMENTS_CSV
    out: Dict[str, Dict] = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                uid = (r.get("veve_uuid") or "").strip()
                if not uid:
                    continue
                lst = (r.get("listings") or "").strip()
                out[uid] = {
                    "name": r.get("name") or uid[:8],
                    "categorie": (r.get("category") or "").strip(),
                    "rarity": r.get("rarity") or "",
                    "edition": r.get("edition_type") or "",
                    "supply": int(_f(r.get("supply"))),
                    "premiere": int(_f(r.get("first_public"))),
                    # VIDE = INCONNU, surtout pas zero.
                    "listings": int(_f(lst)) if lst else None,
                    "note": (r.get("note") or "").strip(),
                    "serie": (r.get("series_uuid") or "").strip(),
                    "marque": (r.get("brand") or "").strip(),
                    "licence": (r.get("licensor") or "").strip(),
                    # ATL/ATH du floor (peuples par preda depuis MyNftTracker).
                    # ATL <= 0 = inconnu ; ATH aberrant (>= ATH_CAP) = inconnu.
                    "atl": (lambda a: a if a > 0 else None)(_f(r.get("atl"))),
                    "ath": (lambda a: a if 0 < a < ATH_CAP else None)(
                        _f(r.get("ath"))),
                }
    except FileNotFoundError:
        print(f"  (pas de {chemin} : catalogue inconnu — signaux comics et "
              f"numeros desactives)", file=sys.stderr)
    return out


def comics_petit_tirage(cat: Dict[str, Dict]) -> Dict[str, Dict]:
    return {u: c for u, c in cat.items()
            if c.get("categorie") == "comic"
            and 0 < c["supply"] <= COMIC_SUPPLY_MAX}


def detect_comics(state: Dict, comics: Dict[str, Dict],
                  veve: Dict[str, float], listings: List[Dict], omi: float,
                  ts: float = None) -> List[Dict]:
    """Un comic a petit tirage sous COMIC_MAX_USD, sur le flux des NOUVELLES
    MISES EN VENTE StackR — et RIEN d'autre.

    ⚠️ Preda (15/07) : « une bonne affaire ne tient pas plus de quelques
    minutes ». On ne balaie donc PLUS l'existant — ni le floor VeVe de tous les
    items (getElements, rafraichi 1x/h : jamais « recent »), ni les offres
    StackR deja memorisees (`sfloors`). Seulement ce qui vient d'etre liste,
    comme la chasse aux numeros. Le floor VeVe ne sert plus qu'a AFFICHER une
    reference sur la carte, jamais a declencher.
    NB : sans cookie, le marche VeVe n'expose aucun flux de nouveaux listings —
    ce signal ne vit donc que sur StackR."""
    ts = ts if ts is not None else time.time()
    if not comics:
        return []
    vus: Dict[str, float] = state.setdefault("comics_vus", {})
    trouve: Dict[str, Dict] = {}
    ecartes: List = []

    def _garder(uid, prix, ou):
        c = comics[uid]
        # ⚠️ LA PROFONDEUR DU CARNET. Huit offres au meme prix, ce n'est pas une
        # aubaine : c'est le prix du marche. On ne retient que l'offre RARE.
        n = c.get("listings")
        if n is not None and n > COMIC_MAX_LISTINGS:
            ecartes.append((c["name"], n))
            return
        anc = trouve.get(uid)
        if anc and anc["usd"] <= prix:
            return
        trouve[uid] = {"uuid": uid, "usd": round(prix, 2), "ou": ou,
                       "name": c["name"], "rarity": c["rarity"],
                       "edition": c["edition"], "supply": c["supply"],
                       "serie": c["serie"], "listings": n,
                       "note": c.get("note") or "",
                       "veve_floor": veve.get(uid, 0.0), "atl": c.get("atl")}

    # SEULE SOURCE : les NOUVELLES mises en vente StackR (prix en OMI), fraiches
    # a la minute (flux interroge toutes les 2 min). Aucun balayage de
    # l'existant : une affaire ne survit pas assez longtemps pour ca.
    for it in listings or []:
        uid = str(it.get("element_id") or "")
        if uid not in comics or not omi:
            continue
        usd = _f(it.get("price")) * omi
        if 0 < usd < COMIC_MAX_USD:
            _garder(uid, usd, "StackR")

    if ecartes:
        print(f"  📚 {len(ecartes)} comic(s) ecarte(s) : trop d'offres au meme "
              f"prix (> {COMIC_MAX_LISTINGS}) — c'est le prix du marche, pas une "
              f"aubaine. Ex. : "
              + ", ".join(f"{n} ({k} offres)" for n, k in ecartes[:3]),
              flush=True)
    out = [a for uid, a in trouve.items()
           if ts - vus.get(uid, 0) >= COOLDOWN_H * 3600]
    if not out:
        return []
    if len(out) > COMIC_MAX_ALERTES:
        # ⚠️ ON NE MEMORISE RIEN : un garde-fou ne doit jamais detruire ce qu'il
        # protege. Si on notait ces uuid comme « vus », ils seraient enterres
        # pour 6 h et Preda ne les reverrait jamais.
        print(f"  ⛔ {len(out)} comics sous {COMIC_MAX_USD:g} $ — c'est le SEUIL "
              f"qui est trop large, pas une aubaine. RIEN n'est publie ni "
              f"memorise. Baisse COMIC_MAX_USD ou COMIC_SUPPLY_MAX, ou releve "
              f"COMIC_MAX_ALERTES si tu les veux vraiment tous.",
              file=sys.stderr)
        for a in sorted(out, key=lambda x: x["usd"])[:10]:
            print(f"     {a['name'][:40]:<40} {a['usd']:>6.2f} $ · "
                  f"{a['supply']} ex. · {a['ou']}", file=sys.stderr)
        return []
    for a in out:
        vus[a["uuid"]] = ts
    out.sort(key=lambda a: (a["supply"], a["usd"]))   # le plus rare d'abord
    return out


# ⚠️ LE LIEN VEVE D'UN COMIC : la page marche d'une RARETE, donc l'uuid de
# l'ELEMENT — PAS celui de la serie. (Deja paye le 14/07 sur les crafts :
# confondre les deux uuid donne la page de QUELQU'UN D'AUTRE, et un lien qui
# s'ouvre a l'air juste tout en etant faux.)
LIEN_COMIC = "https://www.veve.me/collectibles/en/market/comics/{uuid}"


def carte_comic(a: Dict) -> Dict:
    lien = lien_stackr(a["uuid"], "comic") if a.get("uuid") else ""
    lignes = [f"**Tirage** : {a['supply']:,} exemplaires".replace(",", " "),
              f"**Prix** : **{a['usd']:.2f} $** sur **{a['ou']}**"]
    n = a.get("listings")
    if n is not None:
        lignes.append(f"**Offres en vente** : {n}"
                      + (" — offre unique" if n <= 1 else ""))
    if a.get("note"):
        lignes.append(f"**Classement** : {a['note']}")
    if a.get("veve_floor"):
        lignes.append(f"Floor VeVe : {a['veve_floor']:.2f} $")
    if a.get("atl"):
        lignes.append(f"Plus-bas historique : {a['atl']:.2f} $")
    if a.get("rarity") or a.get("edition"):
        lignes.append(f"{a.get('rarity', '')} {a.get('edition', '')}".strip())
    if lien:
        lignes.append(f"[Voir sur StackR]({lien})")
    return {"title": f"📚 {a['name']}"[:250],
            "description": "\n".join(lignes),
            "color": 0x9B59B6,
            "url": lien or None}


def notify_comics(alerts: List[Dict]) -> int:
    if not alerts:
        return 0
    contenu = (f"📚 **{len(alerts)} comic(s) a petit tirage sous "
               f"{COMIC_MAX_USD:g} $** — "
               + _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC"))
    embeds = [carte_comic(a) for a in alerts[:10]]
    bot_alertes.pousser_lot("comics", alerts[:10], embeds, simuler=SIMULER)
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye]", flush=True)
        for a in alerts[:10]:
            print(f"    📚 {a['name'][:40]:<40} {a['usd']:>6.2f} $ · "
                  f"{a['supply']} ex. · {a['ou']}", flush=True)
        return len(alerts)
    try:
        r = requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        if r.status_code == 429:
            time.sleep(min(_f(r.json().get("retry_after")) + 1, 60))
            requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        print(f"  Discord : {len(alerts)} comic(s) pousse(s).", flush=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"  Discord KO ({e})", flush=True)
    return len(alerts)



class Journal:
    """⚠️ UN GARDE-FOU QUI NE DIT PAS POURQUOI IL BLOQUE EST UN MUR.

    Zero alerte pendant deux jours, et aucun moyen de savoir QUI serrait trop :
    la marge ? le benefice minimum ? l'ecart ? la preuve de vente ? Desserrer au
    hasard, c'est deviner — et si deux verrous cedent en meme temps, on ne saura
    toujours pas lequel bloquait. Alors on MESURE : chaque recale est compte par
    motif, et ceux qui ont manque de peu sont gardes AVEC LEURS CHIFFRES.

    Un run, et on sait exactement quel cran tourner, et de combien."""

    LIBELLES = [
        ("broutille", "trop petits (montant derisoire)"),
        ("sans_floor", "sans floor VeVe connu (invendable en face)"),
        ("illiquide", "ILLIQUIDES : aucune vente reelle vue"),
        ("marge", "marge nette insuffisante"),
        ("profit", "benefice en $ insuffisant"),
        ("sous_cote", "pas assez sous le floor StackR"),
        ("ecart", "ecart entre marches insuffisant"),
        ("cooldown", "deja alerte recemment"),
    ]
    SEUILS = {"marge": ("FLOOR_MARGIN_PCT", MARGIN_PCT, "pts de marge"),
              "profit": ("FLOOR_MIN_PROFIT", MIN_PROFIT, "$ de benefice"),
              "ecart": ("FLOOR_SPREAD_PCT", SPREAD_PCT, "pts d'ecart"),
              "sous_cote": ("FLOOR_DROP_PCT", DROP_PCT, "pts sous le floor"),
              "illiquide": ("FLOOR_SALES_PAGES", SALES_PAGES,
                            "pages d'historique de ventes")}

    def __init__(self):
        self.listings = 0
        self.items = set()
        self.motifs: Dict[str, int] = {}
        self.presque: List[Dict] = []
        self._deja = set()

    def rejet(self, motif: str, cand: Dict = None, cle: str = None) -> None:
        if cle is not None:
            if (motif, cle) in self._deja:
                return          # le meme item recale a chaque tour ne compte qu'une fois
            self._deja.add((motif, cle))
        self.motifs[motif] = self.motifs.get(motif, 0) + 1
        if cand and cand.get("net", 0) > 0:
            self.presque.append(dict(cand, bloque_par=motif))

    def _manque(self, c: Dict) -> str:
        m = c.get("bloque_par")
        if m == "marge":
            return f"il manque {MARGIN_PCT - c['marge']:.1f} pts de marge"
        if m == "profit":
            return f"il manque {MIN_PROFIT - c['net']:.2f} $ de benefice"
        if m == "ecart":
            return f"il manque {SPREAD_PCT - c.get('ecart', 0):.1f} pts d'ecart"
        if m == "illiquide":
            return "AUCUNE VENTE connue — ce n'est pas un seuil, c'est la preuve qui manque"
        if m == "sous_cote":
            return f"il manque {DROP_PCT - c.get('d_stackr', 0):.1f} pts sous le floor StackR"
        return m or ""

    def resume(self) -> str:
        l = ["", "═" * 66,
             "  JOURNAL DES RECALES — pourquoi rien n'est sorti",
             "═" * 66,
             f"  Examines : {self.listings} listing(s) neuf(s) · "
             f"{len(self.items)} item(s) du marche"]
        if not self.motifs:
            l.append("  Aucun candidat ecarte : il n'y avait RIEN a examiner. "
                     "Ce n'est pas un probleme de seuil — c'est la source.")
            return "\n".join(l + ["═" * 66, ""])
        l.append("  Ecartes :")
        for k, lib in self.LIBELLES:
            n = self.motifs.get(k, 0)
            if not n:
                continue
            s = self.SEUILS.get(k)
            reglage = f"   [{s[0]}={s[1]:g}]" if s else ""
            l.append(f"     {n:>5}  {lib}{reglage}")
        # LE VERDICT : quel verrou ecarte le plus de candidats REELS ?
        vrais = {k: v for k, v in self.motifs.items()
                 if k in self.SEUILS and v}
        if vrais:
            pire = max(vrais, key=lambda k: vrais[k])
            s = self.SEUILS[pire]
            l += ["", f"  ➜ LE VERROU QUI BLOQUE LE PLUS : {s[0]} (={s[1]:g}) — "
                      f"{vrais[pire]} candidat(s) ecarte(s) par lui seul."]
            if pire == "illiquide":
                l.append("    ⚠️ Ce n'est PAS un seuil de rentabilite : ces items "
                         "ne se vendent pas (ou pas dans notre fenetre de ~7 j).")
                l.append("    Elargir FLOOR_SALES_PAGES donnerait plus de preuves ; "
                         "baisser les marges ne servirait a RIEN.")
        if self.presque:
            self.presque.sort(key=lambda c: -c.get("net", 0))
            l += ["", "  CEUX QUI ONT MANQUE DE PEU (par benefice) :"]
            for c in self.presque[:8]:
                l.append(f"     {c.get('name', '?')[:34]:<34} achat "
                         f"{c.get('usd', 0):>8,.2f} $ → revente "
                         f"{c.get('ref', 0):>9,.2f} $ · marge "
                         f"{c.get('marge', 0):>6.1f} % · net "
                         f"{c.get('net', 0):>+9,.2f} $")
                l.append(f"     {'':34} ↳ {self._manque(c)}")
        l += ["═" * 66, ""]
        return "\n".join(l)


def _marge(achat_usd: float, floor_veve_usd: float):
    """(benefice net en $, marge en % du capital engage)."""
    if achat_usd <= 0 or floor_veve_usd <= 0:
        return 0.0, 0.0
    net = floor_veve_usd * (1.0 - FEE_PCT / 100.0) - achat_usd
    return net, 100.0 * net / achat_usd
COOLDOWN_H = float(os.environ.get("FLOOR_COOLDOWN_H", "6"))
ELEM_LIMIT = int(os.environ.get("FLOOR_ELEM_LIMIT", "100"))
RETRIES = int(os.environ.get("FLOOR_RETRIES", "6"))
TIMEOUT = int(os.environ.get("FLOOR_TIMEOUT", "45"))
PAUSE = float(os.environ.get("FLOOR_PAUSE", "0.2"))


def _get(proc: str, payload: Optional[Dict], session=None, meta=None):
    """Appel trpc. None = echec definitif (jamais d'exception qui tue le run)."""
    inp: Dict = {"json": payload}
    if meta:
        inp["meta"] = meta
    url = BASE + proc + "?input=" + urllib.parse.quote(
        json.dumps(inp, separators=(",", ":")))
    s = session or requests
    for attempt in range(RETRIES):
        try:
            r = s.get(url, headers={"User-Agent": UA,
                                    "Accept": "application/json"},
                      timeout=TIMEOUT)
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return (r.json().get("result", {}).get("data", {})
                    .get("json"))
        except Exception as e:
            if attempt == RETRIES - 1:
                print(f"    {proc} abandonne : {e}", flush=True)
                return None
            wait = min(60, 3 * (2 ** attempt))
            print(f"    {proc} : {e} — nouvel essai dans {wait} s", flush=True)
            time.sleep(wait)
    return None


def recolte_credible(recus: int, attendu: float,
                     ratio: float = None) -> bool:
    """La recolte de floors est-elle assez complete pour etre crue ?

    PURE, donc testable sans reseau. `attendu` est la meilleure recolte
    connue (elle decroit lentement pour suivre un catalogue qui retrecit
    vraiment). Sans reference, on accepte : c'est le premier run.
    """
    ratio = FLOOR_MIN_RATIO if ratio is None else ratio
    if attendu <= 0:
        return True
    return recus >= attendu * ratio


def attendu_suivant(attendu: float, recus: int) -> float:
    """Met a jour la reference. Monte tout de suite, descend de 0,5 % par
    run — un catalogue qui retrecit vraiment est suivi en quelques jours,
    mais une chute brutale ne fait pas baisser la garde."""
    return max(float(recus), attendu * 0.995)


def _crier(texte: str) -> None:
    """Un cri sur le canal principal quand la COLLECTE elle-meme va mal.

    Ce n'est pas une alerte de marche : c'est le systeme qui dit qu'il ne
    voit plus rien. Sans ca, un blocage se lit comme un marche calme.
    Ne leve jamais : un cri rate ne doit pas tuer le run.
    """
    try:
        requests.post(WEBHOOK, json={"content": texte[:1900],
                                     "allowed_mentions": {"parse": []}},
                      timeout=15)
    except Exception as e:                                  # noqa: BLE001
        print(f"    cri d'alerte non parti : {e}", flush=True)


def _f(x) -> float:
    try:
        return float(str(x).replace(",", ".") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def fetch_listings(session=None, limit: int = N_LISTINGS) -> List[Dict]:
    """Les derniers listings (le bloc `meta` est OBLIGATOIRE)."""
    d = _get("getAllLatestListings_v2",
             {"limit": str(limit), "elementType": None, "rarity": None,
              "edition": None, "sortBy": "timestamp",
              "sortDirection": "desc", "timeframe": "1d",
              "direction": "forward"},
             session,
             meta={"values": {"elementType": ["undefined"],
                              "rarity": ["undefined"],
                              "edition": ["undefined"]}, "v": 1})
    if not d:
        return []
    return d.get("items") or []


def fetch_sales(session=None, limit: int = 50) -> List[Dict]:
    """Les dernieres VENTES REELLES (publicVeve.getAllLatestSales_v2).

    Pourquoi : le floor est un prix DEMANDE. Ce qui prouve qu'un item vaut son
    prix, c'est que quelqu'un l'a PAYE. Verification de Preda (12/07) sur
    Fantastic Four #1 SR : ventes reelles a 2 200 $ (07/07), 2 500 $ (02/07) et
    3 975 $ (13/03) — pendant qu'une offre trainait a 676 $. L'affaire etait
    donc bien reelle, et c'est la VENTE qui le prouve, pas le floor.

    Deux formes d'input essayees (l'endpoint exige le bloc meta superjson ;
    la forme exacte varie selon les champs envoyes)."""
    formes = [
        ({"limit": str(limit), "elementType": None, "rarity": None,
          "edition": None, "sortBy": "timestamp", "sortDirection": "desc",
          "timeframe": "1d", "direction": "forward"},
         {"values": {"elementType": ["undefined"], "rarity": ["undefined"],
                     "edition": ["undefined"]}, "v": 1}),
        ({"limit": str(limit), "timeframe": "1d", "direction": "forward"},
         {"values": {}, "v": 1}),
    ]
    for payload, meta in formes:
        d = _get("getAllLatestSales_v2", payload, session, meta=meta)
        items = (d or {}).get("items") if isinstance(d, dict) else None
        if items:
            return items
    print("    (ventes indisponibles ce tour — alertes basees sur le seul "
          "floor)", flush=True)
    return []


def fetch_history(session=None, pages: int = SALES_PAGES,
                  omi: float = 0.0) -> Dict[str, list]:
    """{uuid -> [prix $ de la DERNIERE vente, jour]} depuis getVeveTransactions.

    ⚠️⚠️ LES DEUX FLUX N'ONT PAS LA MEME UNITE (verifie sur les donnees reelles
    le 14/07, apres deux jours de silence inexplique) :
      * `getAllLatestSales_v2` (ventes StackR) -> prix en **OMI**
        (12 500 OMI = 2,11 $) -> il FAUT multiplier par le cours ;
      * `getVeveTransactions` (ventes VeVe)    -> prix en **DOLLARS**
        (MARKET_FIXED : 3,93 · 3,99 · 29,90 · 2,88) -> AUCUNE conversion.

    On multipliait TOUT par le cours OMI : chaque vente VeVe etait donc divisee
    par ~5 900. Une vente a 2 200 $ devenait 0,37 $ — et comme le prix de revente
    retenu est le MINIMUM entre le floor et la derniere vente, la marge
    s'effondrait a chaque fois. **Les preuves qu'on collectait TUAIENT les
    alertes qu'elles devaient justifier.** Pire : les petites ventes tombaient a
    0,00 apres arrondi et etaient jetees en silence (d'ou « 1 618 elements ont
    une vente reelle (0 nouveaux) » alors que l'etat n'en gardait que 526).

    `omi` reste dans la signature pour ne rien casser, mais N'EST PLUS UTILISE.
    LEÇON : une unite ne se suppose pas. Deux sources, deux unites — toujours."""
    out: Dict[str, list] = {}
    # vol[uid][jour] = NB de ventes completes ce jour-la (pour le pic d'activite).
    # C'est du NOMBRE, pas du prix — et le meme flux le donne sans une requete
    # de plus. On compte donc CHAQUE vente, avant le filtre `uid in out` qui, lui,
    # ne retient que la 1re occurrence (la plus recente) pour le prix de revente.
    vol: Dict[str, Dict[str, int]] = {}
    s = session or requests.Session()
    for page in range(1, pages + 1):
        payload = {"limit": 100}
        if page > 1:
            payload["cursor"] = page
            payload["direction"] = "forward"
        d = _get("getVeveTransactions", payload, s)
        if not d:
            break
        for it in d:
            if str(it.get("veve_type")) not in SALES_TYPES:
                continue
            if str(it.get("status")) != "COMPLETE":
                continue
            uid = str(it.get("element_id") or "")
            pr = _f(it.get("price"))          # DEJA en dollars
            if not uid or pr <= 0:
                continue
            jour = str(it.get("created_at") or "")[:10]
            if jour:
                vol.setdefault(uid, {})
                vol[uid][jour] = vol[uid].get(jour, 0) + 1
            if uid in out:
                continue                  # 1re occurrence = la plus RECENTE
            out[uid] = [round(pr, 2), jour]
        time.sleep(PAUSE)
    return out, vol


def merge_history(state: Dict, hist: Dict[str, list]) -> int:
    """Injecte l'historique dans l'etat (meme format que note_sales)."""
    m: Dict[str, list] = state.setdefault("sales", {})
    n = 0
    for uid, (usd, jour) in hist.items():
        if usd and uid not in m:
            m[uid] = [usd, time.time(), jour]
            n += 1
    return n


def fetch_omi_price(session=None) -> float:
    d = _get("getTokenPrices", None, session,
             meta={"values": ["undefined"], "v": 1})
    return _f((d or {}).get("omiPrice"))


def fetch_veve_floors(session=None) -> Dict[str, float]:
    """{uuid -> floor VeVe (gems ~ $)} via getElements.

    PAGINATION : le parametre est `page` (1-based). `cursor`/`offset`/`skip`
    sont IGNORES EN SILENCE (verifie le 12/07) -> auto-controle : si la page 2
    renvoie la page 1, on abandonne au lieu de croire qu'on a tout."""
    out: Dict[str, float] = {}
    page, total, tete = 1, None, None
    while page <= 200:
        d = _get("getElements", {"limit": ELEM_LIMIT, "page": page}
                 if page > 1 else {"limit": ELEM_LIMIT}, session)
        if d is None:
            page += 1
            continue
        rows = d.get("data") or []
        if not rows:
            break
        if total is None:
            total = int(d.get("totalCount") or 0)
        prem = str(rows[0].get("id") or "")
        if page == 1:
            tete = prem
        elif prem == tete:
            print("  !! PAGINATION getElements CASSEE (page 2 == page 1) — "
                  "floors VeVe ignores ce tour.", flush=True)
            return {}
        for e in rows:
            uid = str(e.get("id") or "")
            if uid:
                out[uid] = _f(e.get("floor_market_price"))
        if total and len(out) >= total:
            break
        page += 1
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------------------
# 🌉 Le pont veille → 🟠H-PRIX
# ---------------------------------------------------------------------------

def _num_feed(v: float):
    """Un entier reste un entier, sinon un flottant a 4 decimales : le CSV est
    relu par du Python cote preda (point decimal), pas par le Sheet FR."""
    f = round(_f(v), 4)
    return int(f) if f == int(f) else f


def _ecrire_hprix_feed(lignes: List[list]) -> None:
    """Append-on-change vers data/hprix_feed.csv, puis auto-trim a
    HPRIX_FEED_DAYS. On reecrit tout le fichier (trie chronologiquement) : le
    fichier reste petit (quelques jours de changements) et le commit n'a qu'a
    reprendre CETTE version complete (cf. step commit du workflow)."""
    import csv as _csv
    entete = ["uuid", "name", "categorie", "floor_usd", "ts"]
    lues: List[list] = []
    try:
        with open(HPRIX_FEED_PATH, encoding="utf-8", newline="") as f:
            r = _csv.reader(f)
            head = next(r, None)
            for row in r:
                if len(row) >= 5:
                    lues.append(row[:5])
    except FileNotFoundError:
        pass
    lues.extend(lignes)
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() - HPRIX_FEED_DAYS * 86400))
    lues = [row for row in lues if row[4] >= cutoff]
    os.makedirs(os.path.dirname(HPRIX_FEED_PATH) or ".", exist_ok=True)
    with open(HPRIX_FEED_PATH, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(entete)
        w.writerows(lues)


def emit_hprix_feed(state: Dict, veve: Dict[str, float],
                    cat: Dict[str, Dict] = None, ts: float = None) -> int:
    """Le PONT : quand le floor VeVe d'un COLLECTIBLE change vs la derniere
    valeur emise, on append une ligne au feed. preda l'ingere dans 🟠H-PRIX.

    ⚠️ MEME unite que H-PRIX (gems ~ $) -> aucune conversion. Trolls ignores.
    COMICS EXCLUS (comic_prices.py en reste proprietaire). On enregistre la
    reference (`hprix_emitted`) MEME quand le pont est OFF, pour ne pas cracher
    tout l'univers au 1er allumage — meme discipline que `vfloors`."""
    ts = ts if ts is not None else time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))
    emis = state.setdefault("hprix_emitted", {})     # {uuid: [floor$, ts]}
    cat = cat or {}
    lignes: List[list] = []
    for uid, vf_ in (veve or {}).items():
        vf = _num_feed(vf_)
        if vf <= 0 or vf >= HPRIX_FEED_CAP:          # vide ou troll : on saute
            continue
        c = cat.get(uid) or {}
        # Sans fiche catalogue on ne connait pas la categorie : par prudence on
        # ne prend QUE ce qu'on sait etre un collectible (les comics = ailleurs).
        if c.get("categorie") != "collectible":
            continue
        anc = emis.get(uid)
        prev = _f(anc[0]) if isinstance(anc, list) and anc else None
        if prev is not None and prev == float(vf):   # aucun changement
            continue
        emis[uid] = [vf, ts]                          # reference systematique
        if not HPRIX_FEED_ON:
            continue                                  # OFF : on note, on n'ecrit pas
        lignes.append([uid, c.get("name") or uid[:8], "collectible", vf, stamp])
    if lignes:
        _ecrire_hprix_feed(lignes)
    return len(lignes)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def note_sales(state: Dict, sales: List[Dict], omi: float,
               ts: float = None) -> int:
    """Memorise la DERNIERE VENTE REELLE de chaque element (prix en $)."""
    ts = ts if ts is not None else time.time()
    m: Dict[str, list] = state.setdefault("sales", {})
    n = 0
    for s_ in sales or []:
        uid = str(s_.get("element_id") or "")
        pr = _f(s_.get("price"))
        if not uid or pr <= 0:
            continue
        usd = pr * omi if omi else 0.0
        anc = m.get(uid)
        # on garde la vente la plus RECENTE
        if not anc or str(s_.get("timestamp") or "") >= str(anc[2] or ""):
            m[uid] = [round(usd, 2), ts, str(s_.get("timestamp") or "")]
            n += 1
    return n


def _revente(vf: float, uid: str, state: Dict):
    """Prix de revente RETENU + derniere vente connue.

    Prudence : si l'item s'est vendu MOINS cher que le floor affiche, c'est ce
    prix-la qui fait foi (le floor n'est qu'une demande). On retient donc le
    plus petit des deux — une alerte doit rester vraie meme au pire."""
    last = (state.get("sales") or {}).get(uid)
    if not last:
        return vf, None
    ls = _f(last[0])
    if ls <= 0:
        return vf, None
    return (min(vf, ls) if vf > 0 else ls), ls


def detect(state: Dict, listings: List[Dict], omi: float,
           veve: Dict[str, float], ts: float = None,
           journal: "Journal" = None) -> List[Dict]:
    """Un listing sous le floor = une affaire. Deux comparaisons :
      * marche StackR : price vs stackr_floor_price (tous deux en OMI) ;
      * marche VeVe   : price converti en $ vs floor VeVe (gems ~ $).
    Anti-bruit : listing deja vu, montant derisoire, cooldown par item.
    Chaque rejet est INSCRIT AU JOURNAL : un module qui se tait doit au moins
    pouvoir dire pourquoi."""
    ts = ts if ts is not None else time.time()
    vus: Dict[str, float] = state.setdefault("vus", {})
    alerts: Dict[str, float] = state.setdefault("alerts", {})
    out: List[Dict] = []
    for it in listings:
        nft = str(it.get("nft_id") or "")
        stamp = str(it.get("timestamp") or "")
        cle = nft + "|" + stamp
        if not nft or cle in vus:
            continue
        vus[cle] = ts
        price = _f(it.get("price"))
        if price <= 0:
            continue
        if journal:
            journal.listings += 1
        usd = price * omi if omi else 0.0
        if usd and usd < MIN_USD:
            if journal:
                journal.rejet("broutille")
            continue                       # broutille : on ignore
        sf = _f(it.get("stackr_floor_price"))
        uid = str(it.get("element_id") or "")
        img = it.get("image_url") or ""
        if uid and sf > 0:
            # on memorise le floor StackR de l'item (pour le signal 3)
            state.setdefault("sfloors", {})[uid] = [
                sf, it.get("name") or uid[:8], it.get("rarity") or "", img]
        vf = veve.get(uid, 0.0)
        d_stackr = (100.0 * (sf - price) / sf) if sf > 0 else 0.0
        d_veve = (100.0 * (vf - usd) / vf) if (vf > 0 and usd > 0) else 0.0
        ref, last = _revente(vf, uid, state)
        net, marge = _marge(usd, ref)
        cand = {"name": it.get("name") or uid[:8], "usd": usd, "ref": ref,
                "net": net, "marge": marge, "ecart": d_veve,
                "d_stackr": d_stackr}
        arbitrage = (marge >= MARGIN_PCT and net >= MIN_PROFIT)
        # Un arbitrage suppose de REVENDRE au floor VeVe. Si l'item n'a AUCUNE
        # vente reelle connue, cette revente est une fiction. La sous-cotation
        # sur le MEME marche (floor StackR), elle, n'a pas besoin de preuve :
        # c'est une comparaison a offre egale.
        preuve_ok = (last is not None) or not REQUIRE_SALE
        sous_cote = d_stackr >= DROP_PCT
        if not sous_cote and not (arbitrage and preuve_ok):
            if arbitrage and not preuve_ok:
                state.setdefault("sans_vente", {})[uid] = ts
                if journal:
                    journal.rejet("illiquide", cand)
            elif journal:
                if vf <= 0:
                    journal.rejet("sans_floor")
                elif marge < MARGIN_PCT:
                    journal.rejet("marge", cand)
                elif net < MIN_PROFIT:
                    journal.rejet("profit", cand)
                else:
                    journal.rejet("sous_cote", cand)
            continue
        if ts - alerts.get(uid or nft, 0) < COOLDOWN_H * 3600:
            if journal:
                journal.rejet("cooldown")
            continue
        alerts[uid or nft] = ts
        out.append({"net": round(net, 2), "marge": round(marge, 1),
                    "revente": round(ref, 2), "last": last, "img": img,
                    "nft": nft, "uuid": uid,
                    "name": it.get("name") or uid[:8],
                    "rarity": it.get("rarity") or "",
                    "edition": it.get("edition"),
                    "price": price, "usd": usd,
                    "stackr_floor": sf, "veve_floor": vf,
                    "d_stackr": round(d_stackr, 1),
                    "d_veve": round(d_veve, 1),
                    "seller": it.get("listed_by_username")
                    or (it.get("listed_by") or "")[:10]})
    # menage de l'etat (on ne garde que 24 h de listings vus)
    for k, t in list(vus.items()):
        if ts - t > 86400:
            vus.pop(k, None)
    out.sort(key=lambda a: -a.get("net", 0))   # par BENEFICE, pas par %
    return out


def detect_spread(state: Dict, veve: Dict[str, float], omi: float,
                  ts: float = None, journal: "Journal" = None) -> List[Dict]:
    """SIGNAL 3 — ecart entre marches, sur les offres DEJA EN PLACE.

    Le floor StackR de chaque item est memorise au fil des listings vus
    (`sfloors` dans l'etat). Si le floor StackR converti en $ est nettement
    sous le floor VeVe, il y a une offre achetable tout de suite ici et
    revendable la-bas."""
    ts = ts if ts is not None else time.time()
    sfloors: Dict[str, list] = state.setdefault("sfloors", {})
    alerts: Dict[str, float] = state.setdefault("alerts", {})
    out: List[Dict] = []
    if not omi:
        return out
    for uid, infos in list(sfloors.items()):
        sf, name, rarity = infos[0], infos[1], infos[2]
        img = infos[3] if len(infos) > 3 else ""
        if journal:
            journal.items.add(uid)
        vf = veve.get(uid, 0.0)
        sf_usd = sf * omi
        if sf_usd <= 0 or sf_usd < MIN_USD:
            if journal:
                journal.rejet("broutille", cle=uid)
            continue
        if vf <= 0:
            if journal:
                journal.rejet("sans_floor", cle=uid)
            continue
        ecart = 100.0 * (vf - sf_usd) / vf
        ref, last = _revente(vf, uid, state)
        net, marge = _marge(sf_usd, ref)
        cand = {"name": name, "usd": sf_usd, "ref": ref, "net": net,
                "marge": marge, "ecart": ecart, "d_stackr": 0.0}
        if ecart < SPREAD_PCT:
            if journal:
                journal.rejet("ecart", cand, cle=uid)
            continue
        if marge < MARGIN_PCT:
            if journal:
                journal.rejet("marge", cand, cle=uid)
            continue
        if net < MIN_PROFIT:
            if journal:
                journal.rejet("profit", cand, cle=uid)
            continue
        if REQUIRE_SALE and last is None:
            # aucune vente depuis ~7 jours : l'item NE SE VEND PAS. Revendre au
            # floor VeVe y est une fiction -> on se tait.
            state.setdefault("sans_vente", {})[uid] = ts
            if journal:
                journal.rejet("illiquide", cand, cle=uid)
            continue
        # MEME verrou que les listings (cle = uuid) : un item deja signale
        # comme listing ne doit PAS ressortir en "ecart de marches" — c'est la
        # MEME affaire vue sous deux angles.
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            if journal:
                journal.rejet("cooldown", cle=uid)
            continue
        alerts[uid] = ts
        out.append({"net": round(net, 2), "marge": round(marge, 1),
                    "revente": round(ref, 2), "last": last, "img": img,
                    "nft": "", "uuid": uid, "name": name, "rarity": rarity,
                    "edition": "", "price": sf, "usd": sf_usd,
                    "stackr_floor": sf, "veve_floor": vf,
                    "d_stackr": 0.0, "d_veve": round(ecart, 1),
                    "seller": "(floor du marche)", "spread": True})
    out.sort(key=lambda a: -a.get("net", 0))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 🎯 LA CHASSE AUX NUMEROS — detection
# ═══════════════════════════════════════════════════════════════════════════

def detect_mints(state, cat, listings, omi, veve=None, dates=None, ts=None):
    """Un numero remarquable VENDU AU PRIX D'UN NUMERO BANAL.

    Les deux moities du signal comptent :
      * le NUMERO est remarquable (#1, 1234, 2001 sur un Star Wars…) — c'est
        `numeros.motifs()` qui tranche, et il ne connait que des faits ;
      * le PRIX est celui d'une edition ordinaire (≤ floor + MINT_MARGE_PCT %) —
        autrement dit **le vendeur n'a pas vu ce qu'il vendait**. Sans cette
        moitie-la, on alerterait sur chaque #1 liste a 50x le floor : du bruit,
        tous les jours, jusqu'a ce que plus personne ne lise les alertes."""
    ts = ts if ts is not None else time.time()
    if not cat or not listings:
        return []
    vus = state.setdefault("mints_vus", {})
    veve = veve or {}
    out = []
    for it in listings:
        uid = str(it.get("element_id") or "")
        c = cat.get(uid)
        if not c:
            continue
        ed = int(_f(it.get("edition")))
        prix = _f(it.get("price"))
        if ed <= 0 or prix <= 0:
            continue
        nft = str(it.get("nft_id") or (uid + "#" + str(ed)))
        if ts - vus.get(nft, 0) < COOLDOWN_H * 3600:
            continue

        annees = nu.annees_pour([c["name"], c.get("marque", ""),
                                 c.get("licence", "")], dates)
        mot = nu.motifs(ed, c["supply"], c["premiere"], annees)
        pts = nu.score(mot)
        if not mot or pts < MINT_SCORE_MIN:
            continue

        # LE PRIX D'UN NUMERO BANAL : on compare au floor du MEME marche
        # (StackR), et a defaut au floor VeVe.
        usd = prix * omi if omi else 0.0
        sf = _f(it.get("stackr_floor_price"))
        floor_usd = (sf * omi) if (sf > 0 and omi) else veve.get(uid, 0.0)
        if floor_usd <= 0 or usd <= 0:
            continue
        if usd > floor_usd * (1 + MINT_MARGE_PCT / 100.0):
            continue                 # le vendeur SAIT ce qu'il a : pas pour nous

        vus[nft] = ts
        out.append({"uuid": uid, "nft": nft, "edition": ed, "atl": c.get("atl"),
                    "usd": round(usd, 2), "prix": prix,
                    "floor": round(floor_usd, 2), "name": c["name"],
                    "rarity": c.get("rarity", ""),
                    "categorie": c.get("categorie", ""),
                    "supply": c["supply"], "note": c.get("note", ""),
                    "serie": c.get("serie", ""), "motifs": mot, "score": pts})

    if len(out) > MINT_MAX_ALERTES:
        # Meme regle que partout : au-dela, c'est le REGLAGE qui est trop large.
        # On ne publie RIEN et **on ne memorise RIEN** — sinon on enterrerait
        # pour 6 h des editions que personne n'aurait vues.
        print("  ⛔ " + str(len(out)) + " numeros remarquables d'un coup : c'est "
              "MINT_SCORE_MIN (=" + str(MINT_SCORE_MIN) + ") qui est trop bas. "
              "RIEN n'est publie ni memorise.", file=sys.stderr)
        for a in out:
            vus.pop(a["nft"], None)
        return []
    out.sort(key=lambda a: -a["score"])
    return out


def carte_mint(a):
    """Le lien pointe sur la RARETE (uuid de l'ELEMENT) : la page marche. Un lien
    qui mene au mauvais item est pire qu'un lien mort — le lien mort, on le
    voit."""
    lien = lien_stackr(a["uuid"], a.get("categorie", ""))
    lignes = ["**#{:,}**".format(a["edition"]).replace(",", " ")
              + " — " + nu.raconter(a["motifs"])]
    if a.get("supply"):
        lignes.append("Tirage : {:,} exemplaires".format(a["supply"])
                      .replace(",", " "))
    lignes.append("**Prix : {:.2f} $** · floor {:.2f} $".format(
        a["usd"], a["floor"]))
    if a.get("note"):
        lignes.append("**Classement** : " + a["note"])
    if a.get("atl"):
        lignes.append("Plus-bas historique : **{:.2f} $**".format(a["atl"]))
    lignes.append("[Voir sur StackR](" + lien + ")")
    return {"title": ("🎯 " + a["name"])[:250], "color": 0xE67E22,
            "description": "\n".join(lignes), "url": lien}


def notify_mints(alerts):
    if not alerts:
        return 0
    contenu = ("🎯 **" + str(len(alerts)) + " numéro(s) remarquable(s)** — "
               + _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC"))
    embeds = [carte_mint(a) for a in alerts[:10]]
    bot_alertes.pousser_lot("mint", alerts[:10], embeds, simuler=SIMULER)
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye]", flush=True)
        for a in alerts[:10]:
            print("    🎯 {:<32} #{:<7} {:>8.2f} $ (floor {:.2f}) · {}".format(
                a["name"][:32], a["edition"], a["usd"], a["floor"],
                nu.raconter(a["motifs"])), flush=True)
        return len(alerts)
    try:
        r = requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        if r.status_code == 429:
            time.sleep(min(_f(r.json().get("retry_after")) + 1, 60))
            requests.post(WEBHOOK, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        print("  Discord : " + str(len(alerts)) + " numero(s) pousse(s).",
              flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("  Discord KO (" + str(e) + ")", flush=True)
    return len(alerts)


# ═══════════════════════════════════════════════════════════════════════════
# LOT 1 — 🩸 LE VOL SUR LE FLOOR VEVE  ·  📈 LA VENTE AU-DESSUS DU FLOOR
# ═══════════════════════════════════════════════════════════════════════════

def lien_marche(uuid: str, categorie: str = "") -> str:
    """La page marche VeVe d'un ELEMENT. comic -> /comics, sinon /collectibles.
    (Meme regle que carte_mint : l'uuid de l'ELEMENT, jamais celui de la serie.)"""
    genre = "comics" if categorie == "comic" else "collectibles"
    return "https://www.veve.me/collectibles/en/market/" + genre + "/" + uuid


def lien_stackr(uuid: str, categorie: str = "") -> str:
    """La page StackR de l'ELEMENT — marche en OMI, onglet Listings + achat direct.
    Quand l'alerte porte un prix EN OMI (numeros, comics, vente ×3), on pointe
    ici et non sur VeVe. Le segment reprend l'element_type StackR (verifie le
    16/07) : COMIC_COVER -> /comic-cover, sinon /collectible. Toujours l'uuid de
    l'ELEMENT (jamais la serie), meme regle que les liens VeVe."""
    genre = "comic-cover" if categorie == "comic" else "collectible"
    return ("https://www.stackr.world/collections/veve/" + genre + "/" + uuid
            + "?table=Listings&action=buy")


def detect_veve_steal(state, veve, cat=None, ts=None):
    """A. Le floor VeVe s'effondre vs sa valeur RECENTE = un vol sur le marche
    ou l'on achete vraiment (single-marche : floor VeVe vs floor VeVe memorise).

    ⚠️ On ENREGISTRE toujours le floor courant (`vfloors`), meme canal eteint —
    ainsi le jour ou Preda l'allume, la reference est deja la et le 1er run
    detecte, au lieu d'attendre un 2e rafraichissement."""
    ts = ts if ts is not None else time.time()
    anc = state.setdefault("vfloors", {})            # {uuid: [floor$, ts]}
    alerts = state.setdefault("alerts_steal", {})
    cat = cat or {}
    out = []
    for uid, vf_ in (veve or {}).items():
        vf = _f(vf_)
        vieux = anc.get(uid)
        prev = _f(vieux[0]) if isinstance(vieux, list) and vieux else 0.0
        anc[uid] = [round(vf, 4), ts]                # <-- enregistrement systematique
        if not STEAL_ON:
            continue
        # 1re observation (pas de reference), plancher plateforme, ou pas de chute
        if prev <= 0 or vf <= PLANCHER_VEVE or vf >= prev:
            continue
        # Un « etait 42 000 milliards » n'est pas une chute, c'est une
        # offre farfelue qui disparait. On se tait plutot que d'annoncer
        # -99,99 %.
        if prev > PRIX_MAX or vf > PRIX_MAX:
            continue
        drop = 100.0 * (prev - vf) / prev
        if drop < STEAL_PCT or (prev - vf) < STEAL_MIN_USD:
            continue
        last = (state.get("sales") or {}).get(uid)
        ls = _f(last[0]) if last else 0.0
        if REQUIRE_SALE and ls <= 0:
            # un floor qui tombe sur un item qui ne se vend jamais n'est pas une
            # affaire : c'est une vitrine sans acheteur.
            state.setdefault("sans_vente", {})[uid] = ts
            continue
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        nom = c.get("name") or (state.get("sfloors", {}).get(uid)
                                or [None, uid[:8]])[1]
        out.append({"uuid": uid, "name": nom,
                    "categorie": c.get("categorie", ""),
                    "floor": round(vf, 2), "avant": round(prev, 2),
                    "drop": round(drop, 1), "atl": c.get("atl"),
                    "last": round(ls, 2) if ls > 0 else None})
    if len(out) > STEAL_MAX:
        # trop d'un coup = un seuil trop bas, pas 20 aubaines. RIEN memorise.
        for a in out:
            alerts.pop(a["uuid"], None)
        print("  ⛔ " + str(len(out)) + " floors VeVe en chute d'un coup : "
              "VEVE_STEAL_PCT (=" + str(STEAL_PCT) + ") trop bas. RIEN publie "
              "ni memorise.", file=sys.stderr)
        return []
    out.sort(key=lambda a: -a["drop"])
    return out


def detect_sale_spike(state, ventes, veve, omi, cat=None, ts=None):
    """B. Une vente REELLE conclue bien au-dessus du floor : la demande chauffe.

    Signal d'information (pas une affaire a saisir). Reference = floor du MEME
    marche (StackR, memorise) de preference, sinon floor VeVe ; tout ramene en
    dollars pour ne jamais melanger OMI et $ (la leçon des unites, v18)."""
    ts = ts if ts is not None else time.time()
    if not ventes or not omi or not SPIKE_ON:
        return []
    cat = cat or {}
    vues = state.setdefault("ventes_vues", {})
    sfloors = state.get("sfloors", {})
    alerts = state.setdefault("alerts_spike", {})
    out = []
    for v in ventes:
        uid = str(v.get("element_id") or "")
        pr = _f(v.get("price"))                       # OMI
        cle = uid + "|" + str(v.get("timestamp") or "")
        if not uid or pr <= 0 or cle in vues:
            continue
        vues[cle] = ts
        sale_usd = pr * omi
        if sale_usd < SPIKE_MIN_USD or sale_usd > PRIX_MAX:
            continue
        sfo = _f((sfloors.get(uid) or [0])[0])
        floor_usd = (sfo * omi) if sfo > 0 else veve.get(uid, 0.0)
        if floor_usd <= 0:
            continue
        ratio = sale_usd / floor_usd
        if ratio < SPIKE_RATIO:
            continue
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        nom = c.get("name") or (sfloors.get(uid) or [None, uid[:8]])[1]
        out.append({"uuid": uid, "name": nom,
                    "categorie": c.get("categorie", ""),
                    "vente": round(sale_usd, 2), "floor": round(floor_usd, 2),
                    "ratio": round(ratio, 1), "edition": v.get("edition"),
                    "atl": c.get("atl")})
    for k, t in list(vues.items()):                  # 24 h de ventes vues
        if ts - t > 86400:
            vues.pop(k, None)
    if len(out) > SPIKE_MAX:
        for a in out:
            alerts.pop(a["uuid"], None)
        print("  ⛔ " + str(len(out)) + " ventes au-dessus du floor d'un coup : "
              "SALE_SPIKE_RATIO (=" + str(SPIKE_RATIO) + ") trop bas. RIEN "
              "publie ni memorise.", file=sys.stderr)
        return []
    out.sort(key=lambda a: -a["ratio"])
    return out


def detect_ath(state, veve, cat=None, ts=None):
    """🆕 Un floor qui BAT son plus-haut historique (ATH tracker, affine en
    direct). Signal marche -> canal dedie. Preuve de vente exigee : un floor
    gonfle sans acheteur ne dit rien."""
    ts = ts if ts is not None else time.time()
    seen = state.setdefault("ath_seen", {})          # {uuid:[floor,ts]} extreme live
    alerts = state.setdefault("alerts_ath", {})
    cat = cat or {}
    out = []
    for uid, vf_ in (veve or {}).items():
        vf = _f(vf_)
        prev = _f((seen.get(uid) or [0])[0])
        ref = _f((cat.get(uid) or {}).get("ath") or 0)
        eff = max(ref, prev)                         # plus-haut connu
        if vf > prev:
            seen[uid] = [round(vf, 4), ts]           # on suit l'extreme live
        if not ATH_ON:
            continue
        if (vf <= PLANCHER_VEVE or vf > PRIX_MAX
                or eff <= 0 or eff > PRIX_MAX
                or vf < eff * (1 + ATH_MARGIN_PCT / 100.0)):
            continue
        last = (state.get("sales") or {}).get(uid)
        ls = _f(last[0]) if last else 0.0
        # 🆕 ATH : une VENTE reelle est TOUJOURS exigee (demande Preda 16/07),
        # independamment du REQUIRE_SALE global. Un floor est un prix DEMANDE,
        # trollable a la hausse ; sans un acheteur qui a vraiment paye, un
        # « nouveau plus-haut » n'est qu'une annonce -> le canal se noyait.
        if ls <= 0:
            continue
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        nom = c.get("name") or (state.get("sfloors", {}).get(uid)
                                or [None, uid[:8]])[1]
        out.append({"uuid": uid, "name": nom, "categorie": c.get("categorie", ""),
                    "floor": round(vf, 2), "ath": round(eff, 2),
                    "last": round(ls, 2) if ls > 0 else None})
    if len(out) > ATH_MAX:
        for a in out:
            alerts.pop(a["uuid"], None)
        print("  ⛔ " + str(len(out)) + " nouveaux ATH d'un coup — anormal. "
              "RIEN publie ni memorise.", file=sys.stderr)
        return []
    out.sort(key=lambda a: -a["floor"])
    return out


def detect_atl(state, veve, cat=None, ts=None, fresh=True):
    """📉 Un floor qui touche son PLUS-BAS historique (ATL tracker, affine en
    direct) = le moins cher jamais vu. Signal d'achat -> canal principal.
    Preuve de vente exigee : un plus-bas sur un item qui ne se vend jamais ne
    vaut rien."""
    ts = ts if ts is not None else time.time()
    seen = state.setdefault("atl_seen", {})
    alerts = state.setdefault("alerts_atl", {})
    cat = cat or {}
    out = []
    for uid, vf_ in (veve or {}).items():
        vf = _f(vf_)
        if vf <= 0:
            continue
        vieux = seen.get(uid)
        prev = _f(vieux[0]) if isinstance(vieux, list) and vieux else 0.0
        ref = _f((cat.get(uid) or {}).get("atl") or 0)
        cands = [x for x in (ref, prev) if x > 0]
        eff = min(cands) if cands else 0.0           # plus-bas connu
        if prev <= 0 or vf < prev:
            seen[uid] = [round(vf, 4), ts]           # on suit l'extreme bas live
        if not ATL_ON:
            continue
        # 📉 ATL : un floor SOUS le plus-bas connu SUFFIT (demande Preda 16/07) —
        # ni marge, ni preuve de vente. Descendre sous l'ATL est deja l'info
        # (contrairement au 🆕 ATH, un plus-bas ne se troll pas : personne ne
        # liste sous le marche par erreur pour tromper).
        if (vf <= PLANCHER_VEVE or vf > PRIX_MAX
                or eff <= 0 or eff > PRIX_MAX or vf >= eff):
            continue
        # 📉 FRAICHEUR : si le refresh precedent est trop vieux (un cron a saute),
        # ce plus-bas a pu survenir il y a bien plus d'une heure -> pas
        # instantane -> on se tait (la reference reste a jour). Demande Preda.
        if not fresh:
            continue
        last = (state.get("sales") or {}).get(uid)
        ls = _f(last[0]) if last else 0.0
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        nom = c.get("name") or (state.get("sfloors", {}).get(uid)
                                or [None, uid[:8]])[1]
        out.append({"uuid": uid, "name": nom, "categorie": c.get("categorie", ""),
                    "floor": round(vf, 2), "atl": round(eff, 2),
                    "last": round(ls, 2) if ls > 0 else None})
    if len(out) > ATL_MAX:
        for a in out:
            alerts.pop(a["uuid"], None)
        print("  ⛔ " + str(len(out)) + " plus-bas d'un coup — anormal. RIEN "
              "publie ni memorise.", file=sys.stderr)
        return []
    out.sort(key=lambda a: a["floor"])
    return out


def carte_steal(a):
    lien = lien_marche(a["uuid"], a.get("categorie", ""))
    lignes = ["Floor VeVe : **{:.2f} $**  —  etait {:.2f} $  (**−{} %**)".format(
        a["floor"], a["avant"], a["drop"])]
    if a.get("last"):
        lignes.append("Derniere vente reelle : **{:.2f} $**".format(a["last"]))
    if a.get("atl"):
        lignes.append("Plus-bas historique : **{:.2f} $**".format(a["atl"]))
    lignes.append("[Voir sur VeVe](" + lien + ")")
    return {"title": ("🩸 " + a["name"])[:250], "color": 0xE74C3C,
            "description": "\n".join(lignes), "url": lien}


def carte_spike(a):
    lien = lien_stackr(a["uuid"], a.get("categorie", ""))
    nom = a["name"] + (" #{}".format(a["edition"]) if a.get("edition") else "")
    lignes = ["Vendu **{:.2f} $**  —  floor {:.2f} $  (**×{}**)".format(
        a["vente"], a["floor"], a["ratio"])]
    if a.get("atl"):
        lignes.append("Plus-bas historique : **{:.2f} $**".format(a["atl"]))
    lignes.append("[Voir sur StackR](" + lien + ")")
    return {"title": ("📈 " + nom)[:250], "color": 0x1ABC9C,
            "description": "\n".join(lignes), "url": lien}


def _notify_lot1(alerts, titre, carte, ligne_sim, webhook=None, canal=None):
    """Un message groupe, 10 cartes max, 429 respecte — comme les autres.
    `webhook` route vers un autre canal (None = webhook principal)."""
    wh = webhook if webhook is not None else WEBHOOK
    if not alerts:
        return 0
    contenu = titre + " — " + _dt.datetime.now(_dt.timezone.utc).strftime(
        "%H:%M UTC")
    embeds = [carte(a) for a in alerts[:10]]
    bot_alertes.pousser_lot(canal, alerts[:10], embeds, simuler=SIMULER)
    if not wh or SIMULER:
        print("  [SIMULATION — rien n'est envoye]", flush=True)
        for a in alerts[:10]:
            print("    " + ligne_sim(a), flush=True)
        return len(alerts)
    try:
        r = requests.post(wh, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        if r.status_code == 429:
            time.sleep(min(_f(r.json().get("retry_after")) + 1, 60))
            requests.post(wh, json={"content": contenu, "embeds": embeds},
                          timeout=20)
        print("  Discord : " + str(len(alerts)) + " carte(s) poussee(s).",
              flush=True)
    except Exception as e:                            # noqa: BLE001
        print("  Discord KO (" + str(e) + ")", flush=True)
    return len(alerts)


def notify_steal(alerts):
    return _notify_lot1(
        alerts,
        "🩸 **" + str(len(alerts)) + " floor(s) VeVe effondre(s)**",
        carte_steal,
        lambda a: "🩸 {:<32} {:>7.2f} $ (etait {:.2f}, −{} %)".format(
            a["name"][:32], a["floor"], a["avant"], a["drop"]),
        canal="steal")


def notify_spike(alerts):
    return _notify_lot1(
        alerts,
        "📈 **" + str(len(alerts)) + " vente(s) au-dessus du floor**",
        carte_spike,
        lambda a: "📈 {:<32} vendu {:>8.2f} $ (floor {:.2f}, ×{})".format(
            a["name"][:32], a["vente"], a["floor"], a["ratio"]),
        webhook=WEBHOOK_ATH,
        canal="spike")


def carte_ath(a):
    lien = lien_marche(a["uuid"], a.get("categorie", ""))
    lignes = ["Floor VeVe : **{:.2f} $** — nouveau **plus-haut historique** "
              "(ancien ATH {:.2f} $)".format(a["floor"], a["ath"])]
    if a.get("last"):
        lignes.append("Derniere vente reelle : **{:.2f} $**".format(a["last"]))
    lignes.append("[Voir sur VeVe](" + lien + ")")
    return {"title": ("🆕 " + a["name"])[:250], "color": 0x9B59B6,
            "description": "\n".join(lignes), "url": lien}


def carte_atl(a):
    lien = lien_marche(a["uuid"], a.get("categorie", ""))
    lignes = ["Floor VeVe : **{:.2f} $** — **plus-bas historique** "
              "(ancien ATL {:.2f} $)".format(a["floor"], a["atl"])]
    if a.get("last"):
        lignes.append("Derniere vente reelle : **{:.2f} $**".format(a["last"]))
    lignes.append("[Voir sur VeVe](" + lien + ")")
    return {"title": ("📉 " + a["name"])[:250], "color": 0x2980B9,
            "description": "\n".join(lignes), "url": lien}


def notify_ath(alerts):
    return _notify_lot1(
        alerts,
        "🆕 **" + str(len(alerts)) + " nouveau(x) plus-haut(s) historique(s)**",
        carte_ath,
        lambda a: "🆕 {:<32} {:>8.2f} $ (ancien ATH {:.2f})".format(
            a["name"][:32], a["floor"], a["ath"]),
        webhook=WEBHOOK_ATH,
        canal="ath")


def notify_atl(alerts):
    return _notify_lot1(
        alerts,
        "📉 **" + str(len(alerts)) + " plus-bas historique(s)**",
        carte_atl,
        lambda a: "📉 {:<32} {:>8.2f} $ (ancien ATL {:.2f})".format(
            a["name"][:32], a["floor"], a["atl"]),
        canal="atl")


def carte_histlow(a):
    lien = lien_marche(a["uuid"], a.get("categorie", ""))
    lignes = ["Floor VeVe : **{:.2f} $** — **{}e percentile** de son histoire "
              "({} points)".format(a["floor"], int(a["rank"]), a.get("n") or 0)]
    if a.get("min") is not None:
        lignes.append("Plus bas jamais vu : **{:.2f} $**  ·  median **{:.2f} $**"
                      .format(a.get("min") or 0, a.get("p50") or 0))
    lignes.append("[Voir sur VeVe](" + lien + ")")
    return {"title": ("📊 " + a["name"])[:250], "color": 0x2980B9,
            "description": "\n".join(lignes), "url": lien}


def notify_histlow(alerts):
    return _notify_lot1(
        alerts,
        "📊 **" + str(len(alerts)) + " floor(s) dans le bas de leur histoire**",
        carte_histlow,
        lambda a: "📊 {:<32} {:>8.2f} $ ({}e pct)".format(
            a["name"][:32], a["floor"], int(a["rank"])),
        canal="histlow")


def carte_vol(a):
    lien = lien_stackr(a["uuid"], a.get("categorie", ""))
    lignes = ["**{} offres** — ×{:.1f} la norme (median {:.0f}, p90 {:.0f})".format(
        a["listings"], a["ratio"], a.get("med") or 0, a.get("p90") or 0),
        "[Voir sur StackR](" + lien + ")"]
    return {"title": ("🔊 " + a["name"])[:250], "color": 0xE67E22,
            "description": "\n".join(lignes), "url": lien}


def notify_vol(alerts):
    return _notify_lot1(
        alerts,
        "🔊 **" + str(len(alerts)) + " anomalie(s) d'offres**",
        carte_vol,
        lambda a: "🔊 {:<32} {} offres (×{:.1f})".format(
            a["name"][:32], a["listings"], a["ratio"]),
        webhook=WEBHOOK_ATH,
        canal="vol")


def detect_atl_stackr(state, flux, omi, cat=None, ts=None):
    """📉 PLUS-BAS StackR FRAIS : un prix StackR (mise en vente ou vente du
    tour) jamais vu aussi bas pour cet item SUR LE MARCHE StackR. Instantane
    (flux 2 min). La reference est le plus-bas StackR observe en direct ($),
    memoire separee `atl_stackr` — JAMAIS le floor VeVe (StackR est moins cher,
    on alerterait en continu). Jamais d'alerte a la 1re observation."""
    ts = ts if ts is not None else time.time()
    seen = state.setdefault("atl_stackr", {})        # {uuid:[low_usd, ts]}
    alerts = state.setdefault("alerts_atl_stackr", {})
    cat = cat or {}
    out = []
    for it in flux or []:
        uid = str(it.get("element_id") or "")
        if not uid:
            continue
        pr = _f(it.get("price"))                       # OMI
        if pr <= 0:
            continue
        usd = pr * omi if omi else 0.0
        if usd <= 0:
            continue
        vieux = seen.get(uid)
        prev = _f(vieux[0]) if isinstance(vieux, list) and vieux else 0.0
        if prev <= 0 or usd < prev:
            seen[uid] = [round(usd, 4), ts]            # on suit le plus-bas live
        if not ATL_STACKR_ON:
            continue
        if usd < ATL_STACKR_MIN_USD:
            continue
        if prev <= 0:                                  # 1re observation : pas de reference
            continue
        if usd >= prev * (1 - ATL_STACKR_MARGIN_PCT / 100.0):
            continue
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        genre = c.get("categorie") or (
            "comic" if str(it.get("element_type") or "") == "COMIC_COVER"
            else "collectible")
        nom = c.get("name") or it.get("name") or uid[:8]
        out.append({"uuid": uid, "name": nom, "categorie": genre,
                    "edition": it.get("edition") or "",
                    "usd": round(usd, 2), "prev": round(prev, 2),
                    "omi": round(pr)})
    if len(out) > ATL_STACKR_MAX:
        for a in out:
            alerts.pop(a["uuid"], None)
        print("  ⛔ " + str(len(out)) + " plus-bas StackR d'un coup — anormal. "
              "RIEN publie ni memorise.", file=sys.stderr)
        return []
    out.sort(key=lambda a: a["usd"])
    return out


def carte_atl_stackr(a):
    lien = lien_stackr(a["uuid"], a.get("categorie", ""))
    nom = a["name"] + (" #{}".format(a["edition"]) if a.get("edition") else "")
    lignes = ["Prix StackR : **{:.2f} $** ({} OMI) — **plus-bas jamais vu** sur "
              "StackR (ancien {:.2f} $)".format(a["usd"], a["omi"], a["prev"]),
              "[Voir sur StackR](" + lien + ")"]
    return {"title": ("📉 " + nom)[:250], "color": 0x2980B9,
            "description": "\n".join(lignes), "url": lien}


def notify_atl_stackr(alerts):
    return _notify_lot1(
        alerts,
        "📉 **" + str(len(alerts)) + " plus-bas StackR frais**",
        carte_atl_stackr,
        lambda a: "📉 {:<32} {:>8.2f} $ (ancien {:.2f})".format(
            a["name"][:32], a["usd"], a["prev"]),
        canal="atl_stackr")


def detect_pic(state, vol, cat=None, ts=None):
    """🔥 Un item DEJA installe dont les VENTES DU JOUR explosent vs sa moyenne
    des jours actifs precedents. Source : `vol[uid][jour]` = nb de ventes
    completes ce jour-la (rempli par fetch_history, zero requete de plus).

    « HORS DROP » EST GRATUIT : un item qui vient de dropper n'a pas de jours
    actifs anterieurs -> baseline vide -> ecarte de lui-meme. Pas besoin de
    connaitre le calendrier des drops.

    Un pic = aujourd'hui >= PIC_RATIO x la moyenne des jours actifs precedents,
    ET >= PIC_MIN_COUNT ventes (plancher absolu : ×3 sur 1 -> 3 n'est pas un
    evenement), ET une baseline >= PIC_MIN_BASE sur >= PIC_MIN_JOURS jours (sans
    quoi le ratio n'a aucun sens)."""
    ts = ts if ts is not None else time.time()
    alerts = state.setdefault("alerts_pic", {})
    cat = cat or {}
    if not PIC_ON or not vol:
        return []
    # « Aujourd'hui » = le jour le PLUS RECENT vu dans TOUT le flux (on ne
    # suppose pas que le flux soit trie : on prend le max).
    tous = [j for m in vol.values() for j in m]
    if not tous:
        return []
    aujourd = max(tous)
    out = []
    for uid, jours in vol.items():
        today = jours.get(aujourd, 0)
        if today < PIC_MIN_COUNT:
            continue
        avant = [n for j, n in jours.items() if j < aujourd]
        if len(avant) < PIC_MIN_JOURS:
            continue                       # pas d'historique -> hors drop, ecarte
        base = sum(avant) / len(avant)
        if base < PIC_MIN_BASE or today < base * PIC_RATIO:
            continue
        if ts - alerts.get(uid, 0) < COOLDOWN_H * 3600:
            continue
        alerts[uid] = ts
        c = cat.get(uid) or {}
        nom = c.get("name") or (state.get("sfloors", {}).get(uid)
                                or [None, uid[:8]])[1]
        out.append({"uuid": uid, "name": nom,
                    "categorie": c.get("categorie", ""),
                    "today": today, "base": round(base, 1),
                    "ratio": round(today / base, 1) if base else 0,
                    "jours": len(avant)})
    if len(out) > PIC_MAX:
        for a in out:
            alerts.pop(a["uuid"], None)
        print("  ⛔ " + str(len(out)) + " pics d'un coup — anormal. RIEN "
              "publie ni memorise.", file=sys.stderr)
        return []
    out.sort(key=lambda a: -a["ratio"])
    return out


def carte_pic(a):
    lien = lien_marche(a["uuid"], a.get("categorie", ""))
    lignes = ["**{}** ventes aujourd'hui — moyenne **{}**/j sur {} jour(s) "
              "actif(s)  (**×{}**)".format(
                  a["today"], a["base"], a["jours"], a["ratio"]),
              "[Voir sur VeVe](" + lien + ")"]
    return {"title": ("🔥 " + a["name"])[:250], "color": 0xE67E22,
            "description": "\n".join(lignes), "url": lien}


def notify_pic(alerts):
    return _notify_lot1(
        alerts,
        "🔥 **" + str(len(alerts)) + " pic(s) d'activite hors drop**",
        carte_pic,
        lambda a: "🔥 {:<32} {:>3} ventes (moy {}/j, ×{})".format(
            a["name"][:32], a["today"], a["base"], a["ratio"]),
        webhook=WEBHOOK_ATH,
        canal="pic")


COULEURS = {"stackr": 0x3498DB,      # bleu   : sous le floor StackR
            "veve": 0x2ECC71,        # vert   : revendable plus cher sur VeVe
            "spread": 0xF1C40F}      # jaune  : ecart entre marches

def _omi(x) -> str:
    return f"{x:,.0f} OMI".replace(",", " ")


def _usd(x) -> str:
    return f"~{x:,.2f} $".replace(",", " ")


def _embed(a: Dict) -> Dict:
    """Une carte par affaire, dans la forme demandee par Preda :

        📉 Sous le Floor
        BB-8 #1559
        −5.4 % sous le floor
        45 400 OMI (~11 $)
        Floor : 48 000 OMI (~11 $)
        Voir sur StackR →
    """
    nom = f"{a['name']}" + (f" #{a['edition']}" if a.get("edition") else "")
    lien = lien_stackr(a['uuid'], a.get('categorie', ''))
    lignes = []
    if a.get("spread"):
        kind, titre = "spread", "↔️ Écart entre marchés"
        lignes.append(f"**{nom}**")
        lignes.append(f"Acheter au floor StackR **{_omi(a['stackr_floor'])}** "
                      f"({_usd(a['usd'])})")
        lignes.append(f"Floor VeVe : **{a['veve_floor']:,.2f} $**"
                      .replace(",", " "))
    elif a.get("d_stackr", 0) >= DROP_PCT:
        kind, titre = "stackr", "📉 Sous le Floor"
        lignes.append(f"**{nom}**")
        lignes.append(f"**−{a['d_stackr']} % sous le floor**")
        lignes.append(f"{_omi(a['price'])} ({_usd(a['usd'])})")
        lignes.append(f"Floor : {_omi(a['stackr_floor'])} "
                      f"({_usd(a['stackr_floor'] * (a['usd'] / a['price']) if a['price'] else 0)})")
    else:
        kind, titre = "veve", "💰 Revendable plus cher sur VeVe"
        lignes.append(f"**{nom}**")
        lignes.append(f"Achat : **{_omi(a['price'])}** ({_usd(a['usd'])})")
        lignes.append(f"Floor VeVe : **{a['veve_floor']:,.2f} $**"
                      .replace(",", " "))
    if a.get("last"):
        lignes.append(f"Dernière vente réelle : **{a['last']:,.2f} $**"
                      .replace(",", " "))
    else:
        lignes.append("*Aucune vente récente vue*")
    if a.get("net", 0) > 0:
        lignes.append(f"→ **+{a['net']:,.2f} $ net (+{a['marge']} %)** "
                      f"après {FEE_PCT} % de frais".replace(",", " "))
    lignes.append(f"[Voir sur StackR →]({lien})")
    e = {"title": titre, "color": COULEURS[kind],
         "description": "\n".join(lignes)[:4000], "url": lien}
    if a.get("img"):
        e["thumbnail"] = {"url": a["img"]}
    return e


def notify(alerts: List[Dict]) -> int:
    """UN message par tour, 10 cartes maximum, et on RESPECTE le 429 de Discord
    (s'obstiner sur un rate limit, c'est ce qui fait bannir un webhook)."""
    if not alerts:
        return 0
    embeds = [_embed(a) for a in alerts[:10]]
    bot_alertes.pousser_lot("affaires", alerts[:10], embeds, simuler=SIMULER)
    contenu = (f"🚨 **{len(alerts)} affaire(s)** — "
               + _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M UTC"))
    if len(alerts) > 10:
        contenu += f" (10 affichées sur {len(alerts)})"
    if not WEBHOOK or SIMULER:
        print("  [SIMULATION — rien n'est envoye"
              + ("" if WEBHOOK else " : pas de DISCORD_WEBHOOK") + "]",
              flush=True)
        for a in alerts[:10]:
            preuve = (f"derniere vente {a['last']:,.2f} $" if a.get("last")
                      else "aucune vente vue")
            print(f"    {a['name']} #{a.get('edition') or '-'} — "
                  f"achat {a['usd']:,.2f} $ · {preuve} · "
                  f"+{a['net']:,.2f} $ net", flush=True)
        return len(alerts)
    for essai in range(3):
        try:
            r = requests.post(WEBHOOK,
                              json={"content": contenu, "embeds": embeds},
                              timeout=20)
            if r.status_code == 429:
                attente = 5.0
                try:
                    attente = float(r.json().get("retry_after", 5)) + 1
                except Exception:
                    pass
                print(f"  Discord : rate limit — pause de {attente:.0f} s.",
                      flush=True)
                time.sleep(min(attente, 60))
                continue
            r.raise_for_status()
            print(f"  Discord : {len(alerts)} alerte(s) poussee(s).",
                  flush=True)
            return len(alerts)
        except Exception as e:
            print(f"  Discord KO ({e})", flush=True)
            if essai == 2:
                break
            time.sleep(5)
    return len(alerts)


def load_state() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"vus": {}, "alerts": {}}


def save_state(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f)


def main() -> int:
    t0 = time.time()
    state = load_state()
    if PURGER_VENTES:
        n = len(state.get("sales") or {})
        state["sales"], state["sans_vente"] = {}, {}
        print(f"🧹 {n} vente(s) collectee(s) avec la mauvaise unite : JETEES. "
              f"L'historique va se reconstruire en dollars.", flush=True)
    journal = Journal()
    cat = charger_elements()
    baselines = pb.load_baselines() if (HISTLOW_ON or VOL_ON) else {}
    if baselines:
        print(f"  📊 baselines de prix chargees : {len(baselines)} items",
              flush=True)
    comics = comics_petit_tirage(cat)
    dates = nu.charger_dates()
    if cat:
        print(f"📚 {len(comics)} comic(s) a tirage ≤ {COMIC_SUPPLY_MAX} — alerte "
              f"entre {PLANCHER_VEVE:g} $ et {COMIC_MAX_USD:g} $ (on ne peut pas "
              f"lister sous {PLANCHER_VEVE:g} $ sur VeVe).", flush=True)
        print(f"🎯 chasse aux numeros : {'ON' if MINT_ON else 'OFF'} sur "
              f"{len(cat)} elements · {len(dates)} serie(s) avec des dates cles.",
              flush=True)
    if not ARBITRAGE_ON:
        print("💤 alertes d'ecart de prix : DESACTIVEES "
              "(FLOOR_ARBITRAGE=true pour les rallumer).", flush=True)
    print("🩸 vol sur le floor VeVe : "
          + ("ON · seuil −" + str(STEAL_PCT) + " %" if STEAL_ON
             else "OFF (VEVE_STEAL_ON=true pour l'allumer ; le floor courant "
                  "est enregistre en attendant)"), flush=True)
    print("📈 vente au-dessus du floor : "
          + ("ON · ×" + str(SPIKE_RATIO) + " → canal dedie" if SPIKE_ON
             else "OFF (SALE_SPIKE_ON=true pour l'allumer)"), flush=True)
    print("🆕 nouveau ATH : "
          + ("ON → canal dedie" if ATH_ON else "OFF (ATH_ON=true)")
          + ("" if os.environ.get("DISCORD_WEBHOOK_ATH")
             else "  (⚠️ DISCORD_WEBHOOK_ATH absent → canal principal)"),
          flush=True)
    print("📉 plus-bas historique : "
          + ("ON (canal principal)" if ATL_ON else "OFF (ATL_ON=true)"),
          flush=True)
    print("📉 plus-bas StackR frais (flux 2 min) : "
          + ("ON (canal principal)" if ATL_STACKR_ON
             else "OFF (ATL_STACKR_ON=true ; la reference StackR est tenue a "
                  "jour en attendant)"),
          flush=True)
    print("🔥 pic d'activite hors drop : "
          + ("ON · ×" + str(PIC_RATIO) + " (min " + str(PIC_MIN_COUNT)
             + " ventes/j) → canal dedie" if PIC_ON
             else "OFF (PIC_ON=true pour l'allumer ; calibre en SIMULER)"),
          flush=True)
    print("🌉 pont veille→🟠H-PRIX : "
          + ("ON → " + HPRIX_FEED_PATH + " (collectibles ; preda l'ingere)"
             if HPRIX_FEED_ON
             else "OFF (HPRIX_FEED_ON=true ; la reference des floors est tenue "
                  "a jour en attendant)"),
          flush=True)
    # 🐋 SUIVI WHALE/TEAM — fusionne dans CE workflow le 17/07 : le flux StackR
    # est deja recupere ici, inutile de le fetch une 2e fois dans un workflow a
    # part. Les detecteurs vivent dans whale_watch (module separe, lisible) ; on
    # les pilote depuis cette boucle. Import DIFFERE : whale_watch importe
    # floor_watch, un import en tete de fichier ferait un cycle.
    from scraper import whale_watch as ww
    ww.SIMULER = ww.SIMULER or SIMULER        # un run floor en simuler simule aussi le whale
    wtracked = ww.charger_tracked() if ww.WHALE_ON else ({}, {})
    whale_actif = ww.WHALE_ON and (wtracked[0] or wtracked[1])
    n_whale = len({id(v) for v in wtracked[0].values()}
                  | {id(v) for v in wtracked[1].values()})
    print("🐋 suivi whale/team : "
          + (f"ON · {n_whale} compte(s) suivi(s) → canal "
             + ("dedie" if os.environ.get("DISCORD_WEBHOOK_WHALE") else "principal")
             if whale_actif
             else "OFF (WHALE_ON=true + comptes tagues dans 🟣C-PSEUDOS)"),
          flush=True)

    s = requests.Session()
    veve: Dict[str, float] = {}
    dernier_refresh = 0.0
    total = 0
    for i in range(1, POLLS + 1):
        omi = fetch_omi_price(s)
        # floors VeVe + historique des ventes : rafraichis 1x/heure
        if time.time() - dernier_refresh > REFRESH_MIN * 60:
            neuf = fetch_veve_floors(s)
            _attendu = _f(state.get("floors_attendus") or 0)
            if neuf and not recolte_credible(len(neuf), _attendu):
                # 🛡️ Recolte maigre : on REFUSE et on garde l'ancienne carte.
                _maigres = int(_f(state.get("floors_maigres") or 0)) + 1
                state["floors_maigres"] = _maigres
                print(f"  🛡️ RECOLTE MAIGRE : {len(neuf)} floors recus pour "
                      f"~{_attendu:.0f} attendus ({len(neuf)/max(_attendu,1)*100:.0f} %). "
                      "Carte precedente CONSERVEE — on prefere des floors d'il y a "
                      "une heure a 98 % d'angles morts. Cause probable : blocage, "
                      "coupure reseau, ou API en panne.", flush=True)
                if _maigres >= FLOOR_MAIGRE_ALERTE and WEBHOOK and not SIMULER:
                    _crier(f"🛡️ **Collecte en difficulte** — {_maigres} "
                           f"rafraichissements maigres d'affilee "
                           f"({len(neuf)}/{_attendu:.0f} floors). Les alertes de "
                           "prix tournent sur des donnees figees. A regarder.")
                neuf = None
            if neuf:
                state["floors_attendus"] = attendu_suivant(_attendu, len(neuf))
                state["floors_maigres"] = 0
                veve = neuf
                dernier_refresh = time.time()
                _prev_ref = _f(state.get("last_refresh_ts") or 0)
                _atl_fresh = (_prev_ref > 0
                              and dernier_refresh - _prev_ref
                              <= ATL_FRESH_MIN * 60)
                state["last_refresh_ts"] = dernier_refresh
                if ATL_ON and _prev_ref > 0 and not _atl_fresh:
                    print("  📉 plus-bas VeVe : refresh precedent trop vieux "
                          f"({(dernier_refresh - _prev_ref) / 60:.0f} min > "
                          f"{ATL_FRESH_MIN:g}) — un plus-bas ne serait pas "
                          "instantane, on se tait (reference mise a jour).",
                          flush=True)
                print(f"  floors VeVe rafraichis : {len(veve)} elements.",
                      flush=True)
                # 🌉 LE PONT → 🟠H-PRIX : on ecrit les floors collectibles qui ont
                # CHANGE dans data/hprix_feed.csv (preda l'ingere). Reference
                # tenue a jour meme quand le pont est OFF.
                nf = emit_hprix_feed(state, veve, cat)
                if HPRIX_FEED_ON:
                    print(f"  🌉 pont H-PRIX : {nf} floor(s) collectible(s) "
                          f"change(s) → {HPRIX_FEED_PATH}.", flush=True)
                # 🩸 LE VOL SUR LE FLOOR VEVE — se juge d'un rafraichissement a
                # l'autre. detect_veve_steal enregistre TOUJOURS le floor
                # courant, meme canal eteint (reference prete pour le jour ou
                # Preda l'allume).
                stl = detect_veve_steal(state, veve, cat)
                if stl:
                    if budget(state, WEBHOOK) > 0:
                        print(f"  🩸 {len(stl)} floor(s) VeVe effondre(s) !",
                              flush=True)
                        total += notify_steal(stl)
                        consommer(state, WEBHOOK)
                    else:
                        for a in stl:
                            state.get("alerts_steal", {}).pop(a["uuid"], None)
                        print("  🔇 plafond atteint — vols floor VeVe gardes "
                              "pour plus tard (rien n'est enterre).", flush=True)
                # 🆕 NOUVEAU ATH (canal dedie) · 📉 PLUS-BAS HISTORIQUE (principal).
                # Comme le 🩸, l'extreme live est TOUJOURS enregistre (meme OFF).
                for _lib, _det, _notif, _chan, _wh in (
                        ("🆕 ATH", detect_ath, notify_ath, "alerts_ath", WEBHOOK_ATH),
                        ("📉 plus-bas", detect_atl, notify_atl, "alerts_atl", WEBHOOK)):
                    _res = (_det(state, veve, cat, fresh=_atl_fresh)
                            if _det is detect_atl
                            else _det(state, veve, cat))
                    if not _res:
                        continue
                    if budget(state, _wh) > 0:
                        print(f"  {_lib} : {len(_res)} signal(s) !", flush=True)
                        total += _notif(_res)
                        consommer(state, _wh)
                    else:
                        for _a in _res:
                            state.get(_chan, {}).pop(_a["uuid"], None)
                        print(f"  🔇 plafond atteint — {_lib} gardes pour "
                              f"plus tard.", flush=True)
            hist, vol = fetch_history(s, SALES_PAGES, omi)
            n_h = merge_history(state, hist)
            print(f"  historique : {len(hist)} elements ont une vente reelle "
                  f"({n_h} nouveaux) — les autres sont juges illiquides.",
                  flush=True)
            # 🔥 PIC D'ACTIVITE HORS DROP (canal dedie) — les ventes DU JOUR d'un
            # item deja installe explosent vs sa moyenne. Baseline = `vol` (meme
            # flux, zero requete de plus). Un drop frais n'a pas de baseline ->
            # ecarte tout seul (« hors drop » gratuit).
            pics = detect_pic(state, vol, cat)
            if pics:
                if budget(state, WEBHOOK_ATH) > 0:
                    print(f"  🔥 {len(pics)} pic(s) d'activite hors drop !",
                          flush=True)
                    total += notify_pic(pics)
                    consommer(state, WEBHOOK_ATH)
                else:
                    for a in pics:
                        state.get("alerts_pic", {}).pop(a["uuid"], None)
                    print("  🔇 plafond atteint — pics d'activite gardes pour "
                          "plus tard (rien n'est enterre).", flush=True)
                # 📊 PLUS-BAS DE DISTRIBUTION (percentile multi-annees → canal
                # principal) · 🔊 ANOMALIE D'OFFRES (→ canal dedie). Lisent le
                # magasin de prix ; renvoient [] si baselines vide (donc rien
                # tant que prices-full/les vars ne sont pas branches).
                hl = pb.detect_hist_low(
                    state, veve, baselines, cat, state.get("sales"),
                    on=HISTLOW_ON, pct=HISTLOW_PCT, plancher=PLANCHER_VEVE,
                    cooldown_h=COOLDOWN_H, maxn=ATL_MAX, min_points=HISTLOW_MIN_PTS)
                if hl:
                    if budget(state, WEBHOOK) > 0:
                        print(f"  📊 {len(hl)} floor(s) dans le bas de leur "
                              f"histoire !", flush=True)
                        total += notify_histlow(hl)
                        consommer(state, WEBHOOK)
                    else:
                        for a in hl:
                            state.get("alerts_histlow", {}).pop(a["uuid"], None)
                        print("  🔇 plafond atteint — 📊 gardes pour plus tard.",
                              flush=True)
                lm = {u: c["listings"] for u, c in cat.items()
                      if c.get("listings") is not None}
                va = pb.detect_vol_anomaly(
                    state, lm, baselines, cat, on=VOL_ON, ratio=VOL_RATIO,
                    cooldown_h=COOLDOWN_H, maxn=ATL_MAX)
                if va:
                    if budget(state, WEBHOOK_ATH) > 0:
                        print(f"  🔊 {len(va)} anomalie(s) d'offres !", flush=True)
                        total += notify_vol(va)
                        consommer(state, WEBHOOK_ATH)
                    else:
                        for a in va:
                            state.get("alerts_vol", {}).pop(a["uuid"], None)
                        print("  🔇 plafond atteint — 🔊 gardes pour plus tard.",
                              flush=True)
            # 🐋 GROS TRANSFERTS on-chain (wallet-a-wallet) des comptes suivis —
            # meme cadence horaire que le refresh (collectscan est public, on
            # reste poli). → canal whale dedie.
            if whale_actif:
                wx = ww.detect_transferts(state, wtracked)
                if wx:
                    total += ww.notifier(state, wx)
                    print(f"  🐋 {len(wx)} gros transfert(s) compte suivi.",
                          flush=True)
        ventes = fetch_sales(s)                       # ventes REELLES (OMI)
        nv = note_sales(state, ventes, omi)
        listings = fetch_listings(s)
        if not listings:
            print(f"  [{i}/{POLLS}] aucun listing recu — on reessaiera.",
                  flush=True)
        else:
            # detect() tient l'etat a jour (les floors StackR memorises) meme
            # quand l'arbitrage est eteint : le signal comics s'en sert.
            a = detect(state, listings, omi, veve, journal=journal)
            if SPREAD_ON:
                a += detect_spread(state, veve, omi, journal=journal)
            if not ARBITRAGE_ON:
                rendre(state, a)     # rien n'est publie, rien n'est enterre
                a = []
            print(f"  [{i}/{POLLS}] {len(listings)} listings, {nv} vente(s), "
                  f"{len(a)} alerte(s), OMI={omi:.6f} $", flush=True)
            if a:
                if budget(state, WEBHOOK) > 0:
                    total += notify(a)
                    consommer(state, WEBHOOK)
                else:
                    rendre(state, a)      # RIEN n'est envoye, RIEN n'est enterre
                    print(f"  🔇 plafond de messages atteint — {len(a)} affaire(s) "
                          f"NI publiee(s) NI memorisee(s) : elles ressortiront.",
                          flush=True)
            if MINT_ON:
                m = detect_mints(state, cat, listings, omi, veve, dates)
                if m:
                    if budget(state, WEBHOOK) > 0:
                        print(f"  [{i}/{POLLS}] 🎯 {len(m)} numero(s) "
                              f"remarquable(s) !",
                              flush=True)
                        total += notify_mints(m)
                        consommer(state, WEBHOOK)
                    else:
                        rendre(state, m, comics=True)
                        print(f"  🔇 plafond atteint — {len(m)} numero(s) gardes "
                              f"pour plus tard.", flush=True)
            c = detect_comics(state, comics, veve, listings, omi)
            if c:
                if budget(state, WEBHOOK) > 0:
                    print(f"  [{i}/{POLLS}] 📚 {len(c)} comic(s) a petit tirage "
                          f"sous {COMIC_MAX_USD:g} $ !", flush=True)
                    total += notify_comics(c)
                    consommer(state, WEBHOOK)
                else:
                    rendre(state, c, comics=True)
                    print(f"  🔇 plafond atteint — {len(c)} comic(s) gardes pour "
                          f"plus tard (rien n'est enterre).", flush=True)
            # 📈 LA VENTE AU-DESSUS DU FLOOR — sur le flux des ventes du tour.
            sp = detect_sale_spike(state, ventes, veve, omi, cat)
            if sp:
                if budget(state, WEBHOOK_ATH) > 0:
                    print(f"  [{i}/{POLLS}] 📈 {len(sp)} vente(s) tres au-dessus "
                          f"du floor !", flush=True)
                    total += notify_spike(sp)
                    consommer(state, WEBHOOK_ATH)
                else:
                    for a in sp:
                        state.get("alerts_spike", {}).pop(a["uuid"], None)
                    print("  🔇 plafond atteint — ventes notables gardees pour "
                          "plus tard (rien n'est enterre).", flush=True)
            # 📉 PLUS-BAS StackR FRAIS (flux 2 min = instantane) — sur les mises
            # en vente ET les ventes du tour. Reference = plus-bas StackR observe
            # en direct ($), memoire separee ; canal principal.
            atls = detect_atl_stackr(state, listings + ventes, omi, cat)
            if atls:
                if budget(state, WEBHOOK) > 0:
                    print(f"  [{i}/{POLLS}] 📉 {len(atls)} plus-bas StackR "
                          f"frais !", flush=True)
                    total += notify_atl_stackr(atls)
                    consommer(state, WEBHOOK)
                else:
                    for a in atls:
                        state.get("alerts_atl_stackr", {}).pop(a["uuid"], None)
                    print("  🔇 plafond atteint — plus-bas StackR gardes pour "
                          "plus tard (rien n'est enterre).", flush=True)
            # 🐋 ACHATS / VENTES / MISES EN VENTE des comptes suivis, sur le MEME
            # flux (listings + ventes) deja recupere ce tour → canal whale dedie.
            if whale_actif:
                wm = ww.detect_marche(state, listings, ventes, wtracked, omi)
                if wm:
                    print(f"  [{i}/{POLLS}] 🐋 {len(wm)} evenement(s) marche "
                          f"compte suivi.", flush=True)
                    total += ww.notifier(state, wm)
            save_state(state)
        if i < POLLS:
            time.sleep(INTERVAL_S)
    print(f"Termine : {POLLS} tours, {total} alerte(s), "
          f"{time.time() - t0:.0f}s.", flush=True)
    # ZERO ALERTE N'EST PAS UNE REPONSE : le journal dit QUEL verrou a serre.
    print(journal.resume(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# FIN floor_watch.py v22 — comics : uniquement les nouvelles mises en vente
# StackR (plus de balayage de l'existant). // v21 — LOT 1 : 🩸 vol sur le floor VeVe + 📈 vente au-dessus
# du floor (deux canaux single-marche, ETEINTS par defaut, calibres un a un).
# FIN floor_watch.py v19 — chasse aux numeros · plancher VeVe a 1 $ ·
# ecarts de prix eteints · plus d'avertissement
# v18 — LES DEUX FLUX N'ONT PAS LA MEME UNITE :
# ventes VeVe en DOLLARS, ventes StackR en OMI.
# FIN floor_watch.py v16 (profondeur du carnet + note de classement + budget de
# messages : le spam est structurellement impossible)
# v15 (le lien comic = uuid de l'ELEMENT, pas de la serie)
# v14 (+ signal 4 : le comic a petit tirage brade)\n# v13 — le module DIT pourquoi il se tait (journal des
# recales) et sait tourner a blanc (FLOOR_SIMULER) : on regle sur des chiffres,
# pas sur des suppositions.
