# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_whale_debordement.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🐋 Le debordement etale, il n'enterre pas — et surtout il MEMORISE.

CE QUE CES TESTS GARDENT
------------------------
Un log de prod du 20/07 repetait, 25 fois par run, a l'identique :

    ⛔ 26 evenements comptes suivis d'un coup — anormal. RIEN publie ni memorise.

`detect_marche` et `detect_transferts` faisaient `return []` SANS ecrire dans
`vus`. Les memes evenements etaient donc redecouverts au tour suivant, le lot
restait au-dessus du plafond, et etait rejete a nouveau. 🐋 ne publiait plus
rien — indefiniment, et en silence pour qui ne lit pas stderr.

⭐ `test_ancien_defaut_est_corrige` est LA porte : il enchaine deux tours et
exige que le second publie enfin quelque chose. Si quelqu'un remet un
`return []` sans memorisation, c'est lui qui tombe.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import whale_watch as ww  # noqa: E402

# ⚠️ Horodatages REELS. Piege paye une fois ailleurs : avec des ts petits
# (1000, 2000…) tout tombe dans le cooldown de l'epoque et rien ne se declenche.
T0 = 1_750_000_000


def _fiche(nom="whale1", typ="VeVe Team"):
    return {"username": nom, "type": typ}


def _tracked(wallet="0xabc", nom="whale1"):
    f = _fiche(nom)
    return ({wallet: f}, {nom: f})


def _vente(n, prix, compte="whale1"):
    """Une vente du flux StackR, achetee par un compte suivi."""
    return {"nft_id": f"nft{n}", "timestamp": f"2026-07-20T10:{n:02d}:00Z",
            "element_id": f"uuid{n}", "element_type": "COLLECTIBLE",
            "name": f"Item {n}", "price": prix,
            "buyer": "0xabc", "buyer_username": compte}


# ---------------------------------------------------------------------------
# Sous le plafond : rien ne change
# ---------------------------------------------------------------------------

def test_sous_le_plafond_tout_passe_et_tout_est_memorise():
    state, tracked = {}, _tracked()
    ventes = [_vente(i, 100) for i in range(3)]
    out = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    assert len(out) == 3
    assert len(state["whale_vus"]) == 3, "ce qui est publie doit etre memorise"


def test_un_evenement_deja_vu_ne_repart_pas():
    state, tracked = {}, _tracked()
    ventes = [_vente(i, 100) for i in range(3)]
    ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    encore = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    assert encore == [], "le dedoublonnage par (genre, nft_id, timestamp) tient"


# ---------------------------------------------------------------------------
# ⭐ Au-dessus du plafond : LE CORRECTIF
# ---------------------------------------------------------------------------

def test_debordement_publie_le_plafond_et_pas_zero():
    state, tracked = {}, _tracked()
    ventes = [_vente(i, 100) for i in range(26)]      # les 26 du log de prod
    out = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    assert len(out) == ww.MAX_CARTES, "on publie le plafond, PAS []"


def test_debordement_memorise_ce_qui_est_publie():
    state, tracked = {}, _tracked()
    ventes = [_vente(i, 100) for i in range(26)]
    out = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    vus = state["whale_vus"]
    assert len(vus) == ww.MAX_CARTES
    for c in out:
        assert c["cle"] in vus, "publie sans etre memorise = republie au tour suivant"


def test_debordement_ne_memorise_PAS_le_surplus():
    """Ce qui n'est pas publie doit revenir. Rien n'est enterre."""
    state, tracked = {}, _tracked()
    ventes = [_vente(i, 100) for i in range(26)]
    out = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    publies = {c["cle"] for c in out}
    assert set(state["whale_vus"]) == publies
    assert len(state["whale_vus"]) < 26


def test_les_plus_gros_montants_passent_devant():
    """Si un compte suivi s'agite, on veut ses GROSSES operations — pas les dix
    premieres dans l'ordre du hasard du flux."""
    state, tracked = {}, _tracked()
    ventes = [_vente(i, prix=i * 10) for i in range(26)]   # le 25 est le plus cher
    out = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    montants = [c["usd"] for c in out]
    assert montants == sorted(montants, reverse=True), "tri decroissant"
    assert min(montants) > 0
    # le plus gros du lot (25 * 10 = 250) doit etre dedans
    assert max(montants) == pytest.approx(250.0)


def test_le_debordement_est_annonce_sur_stderr(capsys):
    state, tracked = {}, _tracked()
    ventes = [_vente(i, 100) for i in range(26)]
    ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    err = capsys.readouterr().err
    assert "🔇" in err and "rendus au tour suivant" in err
    assert "RIEN publie ni memorise" not in err, "l'ancien message ne doit plus exister"


# ---------------------------------------------------------------------------
# ⭐⭐ LA PORTE : le defaut historique, reproduit puis exige corrige
# ---------------------------------------------------------------------------

def test_ancien_defaut_est_corrige_le_lot_se_vide_tour_apres_tour():
    """AVANT : 26 evenements -> [] sans memorisation -> les 26 reviennent ->
    encore [] -> a l'infini. 🐋 muet pour toujours.
    APRES : chaque tour en publie 10 et les retire du lot ; le stock devient un
    debit et se vide en 3 tours."""
    state, tracked = {}, _tracked()
    ventes = [_vente(i, prix=i * 10) for i in range(26)]

    t1 = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    t2 = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    t3 = ww.detect_marche(state, [], ventes, tracked, omi=1.0)
    t4 = ww.detect_marche(state, [], ventes, tracked, omi=1.0)

    assert len(t1) == 10
    assert len(t2) == 10, "⛔ le 2e tour rendait [] avant le correctif"
    assert len(t3) == 6, "le reliquat passe sans plafond"
    assert t4 == [], "et le lot est vide"

    # aucun doublon, aucune perte : les 26 sont sortis une fois et une seule
    cles = [c["cle"] for c in t1 + t2 + t3]
    assert len(cles) == 26 and len(set(cles)) == 26


# ---------------------------------------------------------------------------
# Meme regle sur les gros transferts on-chain
# ---------------------------------------------------------------------------

def test_transferts_le_poids_est_le_nombre_de_jetons():
    cand = [{"txh": f"0x{i}", "count": i} for i in range(26)]
    vus = {}
    out = ww._rendre(cand, vus, T0, "txh", lambda c: float(c["count"]),
                     "gros transferts")
    assert len(out) == ww.MAX_CARTES
    assert [c["count"] for c in out] == list(range(25, 15, -1))
    assert len(vus) == ww.MAX_CARTES


def test_rendre_est_stable_si_le_poids_est_absent():
    """Un candidat sans montant ne doit pas faire tomber le detecteur entier."""
    cand = [{"cle": f"k{i}", "usd": None} for i in range(26)]
    vus = {}
    out = ww._rendre(cand, vus, T0, "cle", lambda c: ww.fw._f(c.get("usd")),
                     "evenements")
    assert len(out) == ww.MAX_CARTES
