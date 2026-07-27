# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_retouches_2707.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🔧 LES RETOUCHES DU 27/07/2026 (demandes de Preda), verrouillees par des tests.

QUATRE DEMANDES, ET CE QUI LES PROTEGE ICI :

 1. ETEINDRE 🩸 📈 🆕 📉. Le piege n'etait pas le code mais le WORKFLOW : les
    champs de lancement manuel valaient « true » et passent DEVANT la variable
    de depot. Eteindre par variable laissait donc un « Run workflow » tout
    rallumer. -> `test_les_quatre_canaux_sont_eteints_meme_au_lancement_manuel`

 2. 🎯 « Pour les hauts numeros, seul le dernier du tirage total est
    interessant. » MINT_HAUT passe de 10 a 1 — DANS LE CODE **ET** dans le
    repli en dur du workflow, sinon le second annule le premier (meme famille
    de panne que les 15 reglages non cables du 20/07).

 3. 📚 « Je ne veux qu'acheter des comics entre 0 et 1,99 $ sur VeVe OU
    StackR. » Le detecteur ne lisait plus que StackR depuis le 15/07. Le cote
    VeVe revient — en detecteur de TRANSITION avec amorcage silencieux, car
    66 comics sont deja sous 2 $ et les publier d'un coup bloquerait le canal
    pour toujours (plafond franchi + rien memorise = verrou qui ne s'ouvre
    jamais, defaut deja paye sur 🐋 le 20/07).

 4. 🐋 « Je n'ai d'alerte que pour un seul compte suivi. » Cause racine
    mesuree : dans le flux VeVe, ces comptes ont un pseudo NULL et ne sont
    identifiables que par `veve_user_id` — que le pont n'exportait pas et que
    ce module ne lisait pas. Et ils tradent sur VeVe, pas sur StackR, qui
    etait la seule source lue.
"""

from __future__ import annotations

import os
import re
import time

import pytest

from scraper import floor_watch as fw
from scraper import numeros as nu
from scraper import whale_watch as ww

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(RACINE, ".github", "workflows", "floor-watch.yml")


def _wf() -> str:
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════════════
# 1. LES QUATRE CANAUX ETEINTS
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("champ", ["veve_steal", "sale_spike", "ath_on",
                                   "atl_on"])
def test_les_quatre_canaux_sont_eteints_meme_au_lancement_manuel(champ):
    """⭐ Un champ de lancement a « true » passe DEVANT `vars.X`. Poser la
    variable a false ne suffisait donc pas : un « Run workflow » rallumait le
    canal sans que rien ne le dise. Les defauts doivent etre « false »."""
    bloc = re.search(r"\n      %s:\n((?:        .*\n|\s*#.*\n)+)" % champ,
                     _wf())
    assert bloc, f"champ {champ} introuvable dans le workflow"
    assert 'default: "false"' in bloc.group(1), (
        f"{champ} a encore un defaut « true » : un lancement manuel "
        f"rallumerait ce canal.")


@pytest.mark.parametrize("var", ["VEVE_STEAL_ON", "SALE_SPIKE_ON", "ATH_ON",
                                 "ATL_ON"])
def test_les_replis_en_dur_de_ces_canaux_restent_false(var):
    """L'autre bout : `vars.X || 'false'`. Un repli a 'true' rallumerait le
    canal pour le cron et le pinger."""
    m = re.search(r"\n          %s: .*\|\| '(\w+)' \}\}" % var, _wf())
    assert m, f"{var} n'est pas pose dans le workflow"
    assert m.group(1) == "false", f"{var} a un repli en dur « {m.group(1)} »"


def test_les_detecteurs_eteints_existent_toujours():
    """⛔ On ETEINT, on ne SUPPRIME pas : ces detecteurs enregistrent leur
    reference (floor courant, ATL/ATH vus) meme muets. Les couper ferait
    re-alerter tout le catalogue le jour du rallumage."""
    for nom in ("detect_veve_steal", "detect_sale_spike", "detect_ath",
                "detect_atl"):
        assert hasattr(fw, nom), f"{nom} a disparu — le rallumage serait brutal"


# ═══════════════════════════════════════════════════════════════════════════
# 2. 🎯 SEUL LE DERNIER DU TIRAGE
# ═══════════════════════════════════════════════════════════════════════════

def test_seul_le_dernier_du_tirage_est_un_haut_numero():
    assert nu.HAUT == 1, "MINT_HAUT doit valoir 1 par defaut"
    assert "haut" in nu.motifs(1000, supply=1000)
    for ed in (999, 995, 991, 990):
        assert "haut" not in nu.motifs(ed, supply=1000), (
            f"#{ed}/1000 n'est pas le dernier du tirage")


def test_le_dernier_du_tirage_pese_autant_que_le_numero_1():
    """Un #5000/5000 est le pendant exact du #1 : meme poids, sinon il passe
    sous MINT_SCORE_MIN et ne declenche jamais seul."""
    assert nu.POIDS["haut"] == nu.POIDS["numero_1"] == 6


def test_le_libelle_ne_ment_pas():
    assert nu.LIBELLES["haut"] == "le dernier du tirage"


def test_le_repli_du_workflow_ne_reannule_pas_le_defaut_du_code():
    """⭐ LA LEÇON DU 20/07 : un repli en dur dans le workflow passe DEVANT le
    defaut du code. Changer numeros.py seul n'aurait servi a rien."""
    m = re.search(r"MINT_HAUT: .*\|\| '(\d+)' \}\}", _wf())
    assert m and m.group(1) == "1", "le workflow reimposerait MINT_HAUT=10"


# ═══════════════════════════════════════════════════════════════════════════
# 3. 📚 LE COTE VEVE DES COMICS
# ═══════════════════════════════════════════════════════════════════════════

def _comics(n=3, supply=1000, listings=0):
    return {f"u{i}": {"name": f"Comic {i}", "rarity": "COMMON", "edition": "FE",
                      "supply": supply, "serie": "s", "listings": listings,
                      "note": "", "atl": 9.0, "categorie": "comic"}
            for i in range(n)}


def test_le_premier_balayage_amorce_en_silence():
    """⭐ 66 comics sont deja sous 2 $. Les publier d'un coup depasserait
    COMIC_MAX_ALERTES, le garde-fou publierait zero SANS rien memoriser, et le
    meme lot reviendrait a chaque balayage : un verrou qui ne s'ouvre jamais."""
    state = {}
    comics = _comics(3)
    veve = {"u0": 1.50, "u1": 1.20, "u2": 40.0}
    assert fw.detect_comics_veve(state, comics, veve) == []
    assert state["comics_veve_amorce"] is True
    assert set(state["comics_veve_dedans"]) == {"u0", "u1"}


def test_ensuite_seule_une_ENTREE_dans_la_fenetre_alerte():
    state = {}
    comics = _comics(3)
    veve = {"u0": 1.50, "u1": 1.20, "u2": 40.0}
    fw.detect_comics_veve(state, comics, veve)          # amorcage
    assert fw.detect_comics_veve(state, comics, veve) == []   # rien ne bouge
    veve["u2"] = 1.75                                    # u2 ENTRE
    out = fw.detect_comics_veve(state, comics, veve)
    assert [a["uuid"] for a in out] == ["u2"]
    assert out[0]["ou"] == "VeVe" and out[0]["usd"] == 1.75


def test_un_comic_deja_dedans_ne_realerte_pas_a_chaque_balayage():
    state = {}
    comics, veve = _comics(1), {"u0": 40.0}
    fw.detect_comics_veve(state, comics, veve)
    veve["u0"] = 1.10
    assert len(fw.detect_comics_veve(state, comics, veve)) == 1
    for _ in range(5):
        assert fw.detect_comics_veve(state, comics, veve) == []


def test_le_plancher_plateforme_est_dit_et_non_deguise_en_aubaine():
    """⚠️ On ne peut pas lister sous 1 $ sur VeVe : 1,00 $ est le MINIMUM
    autorise, pas une decote. Preda veut voir la fenetre 0–1,99 — mais la
    carte doit le dire."""
    state = {}
    comics, veve = _comics(1), {"u0": 40.0}
    fw.detect_comics_veve(state, comics, veve)
    veve["u0"] = 1.00
    out = fw.detect_comics_veve(state, comics, veve)
    assert out and out[0]["plancher"] is True
    assert "minimum autorise" in fw.carte_comic(out[0])["description"]


def test_la_carte_veve_pointe_vers_veve_pas_vers_stackr():
    """Un lien qui s'ouvre tout en etant faux est pire qu'un lien absent."""
    state = {}
    comics, veve = _comics(1), {"u0": 40.0}
    fw.detect_comics_veve(state, comics, veve)
    veve["u0"] = 1.30
    carte = fw.carte_comic(fw.detect_comics_veve(state, comics, veve)[0])
    assert "veve.me" in carte["url"] and "stackr" not in carte["url"]
    assert "[Voir sur VeVe]" in carte["description"]


def test_le_carnet_profond_ecarte_aussi_cote_veve():
    """« 8 offres a 1,75 $ » n'est pas une aubaine, c'est le prix du marche —
    la regle vaut sur les deux marches."""
    state = {}
    comics = _comics(1, listings=fw.COMIC_MAX_LISTINGS + 5)
    veve = {"u0": 40.0}
    fw.detect_comics_veve(state, comics, veve)
    veve["u0"] = 1.30
    assert fw.detect_comics_veve(state, comics, veve) == []


def test_un_prix_farfelu_ne_passe_pas():
    state = {}
    comics, veve = _comics(1), {"u0": 40.0}
    fw.detect_comics_veve(state, comics, veve)
    veve["u0"] = 0.0                      # floor inconnu, pas « gratuit »
    assert fw.detect_comics_veve(state, comics, veve) == []


def test_le_cote_veve_est_pilotable_depuis_le_workflow():
    assert "COMIC_VEVE_ON" in _wf()


# ═══════════════════════════════════════════════════════════════════════════
# 4. 🐋 L'IDENTIFICATION DES COMPTES SUIVIS
# ═══════════════════════════════════════════════════════════════════════════

CSV_ENTETE = ("username,type,veve_user_id,wallet_imx,wallet_stackr,"
              "holdings,value_floor\n")


def _csv(tmp_path, lignes):
    p = tmp_path / "tracked.csv"
    p.write_text(CSV_ENTETE + lignes, encoding="utf-8")
    return str(p)


def _tx(uid_acheteur="", addr="0xaaa", prix="12.50", veve_id="tx1"):
    return {"veve_id": veve_id, "status": "COMPLETE", "veve_type":
            "MARKET_FIXED", "created_at": "2026-07-27T10:00:00.000Z",
            "buyer_id": uid_acheteur, "buyer_username": None,
            "buyer_address": addr, "seller_id": "autre",
            "seller_username": None, "seller_address": "0xbbb",
            "nft_id": "n1", "nft_issue": 42, "element_id": "e1",
            "element_type": "COMIC_COVER", "name": "Un comic",
            "price": prix}


def test_un_compte_sans_wallet_ni_pseudo_est_reconnu_par_son_veve_user_id(tmp_path):
    """⭐⭐ LA CAUSE RACINE. Dans le flux VeVe, `buyer_username` est NULL pour
    ces comptes : le veve_user_id est la SEULE cle qui matche. Sans ce test,
    la regression est silencieuse — exactement comme le defaut d'origine."""
    chemin = _csv(tmp_path, "Omegatron88,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    assert len(tracked) == 3 and tracked[2].get("UID-1")
    out = ww.detect_veve({}, [_tx("UID-1")], tracked)
    assert len(out) == 1 and out[0]["compte"] == "Omegatron88"
    assert out[0]["marche"] == "VeVe"


def test_le_wallet_est_appris_a_la_premiere_transaction(tmp_path):
    """Sans wallet, aucun gros transfert on-chain n'est visible. Le resoudre
    cote StackR demande un cookie perissable ; ici c'est gratuit."""
    chemin = _csv(tmp_path, "Omegatron88,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    state = {}
    ww.detect_veve(state, [_tx("UID-1", addr="0xCAFE")], tracked)
    assert state["whale_wallets"]["UID-1"] == "0xcafe"
    assert "0xcafe" in tracked[0], "le wallet doit servir DES CE RUN"


def test_le_wallet_appris_survit_au_run_suivant(tmp_path):
    chemin = _csv(tmp_path, "Omegatron88,Modération,UID-1,,,,\n")
    state = {"whale_wallets": {"UID-1": "0xcafe"}}
    tracked = ww.charger_tracked(chemin)
    assert ww.restaurer_wallets(state, tracked) == 1
    assert tracked[0]["0xcafe"]["username"] == "Omegatron88"


def test_on_n_apprend_jamais_une_adresse_systeme(tmp_path):
    """L'escrow du marche, le mint (0x0) et le burn ne sont pas des wallets."""
    chemin = _csv(tmp_path, "Omegatron88,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    state = {}
    for sale in ww.SYSTEM:
        ww.apprendre_wallet(state, tracked, tracked[2]["UID-1"], sale)
    assert not state.get("whale_wallets")


def test_le_prix_du_flux_veve_est_deja_en_dollars(tmp_path):
    """⚠️ getVeveTransactions donne des DOLLARS, le flux StackR des OMI.
    Confondre les deux a deja coute cher (leçon des unites)."""
    chemin = _csv(tmp_path, "X,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    out = ww.detect_veve({}, [_tx("UID-1", prix="12.50")], tracked)
    assert out[0]["usd"] == 12.50 and out[0]["omi"] == 0


def test_une_vente_farfelue_ne_passe_pas(tmp_path):
    """Une seule carte « vendu 18 666 667 $ » decredibilise un canal entier."""
    chemin = _csv(tmp_path, "X,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    assert ww.detect_veve({}, [_tx("UID-1", prix="18666667")], tracked) == []


def test_pas_de_doublon_entre_deux_tours(tmp_path):
    chemin = _csv(tmp_path, "X,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    state = {}
    txs = [_tx("UID-1")]
    assert len(ww.detect_veve(state, txs, tracked)) == 1
    assert ww.detect_veve(state, txs, tracked) == []


def test_une_transaction_non_conclue_est_ignoree(tmp_path):
    chemin = _csv(tmp_path, "X,Modération,UID-1,,,,\n")
    tracked = ww.charger_tracked(chemin)
    tx = _tx("UID-1")
    tx["status"] = "PENDING"
    assert ww.detect_veve({}, [tx], tracked) == []


def test_le_journal_dit_qui_est_insuivable(tmp_path, capsys):
    """⭐ Un canal muet ne disait pas s'il etait muet parce que le marche
    dormait ou parce que les comptes n'avaient aucune cle. Deux causes, un
    symptome, deux remedes opposes."""
    chemin = _csv(tmp_path, "Muet,Modération,,,,,\nVu,Modération,UID-2,,,,\n")
    tracked = ww.charger_tracked(chemin)
    ww.journal_identite({}, tracked)
    sortie = capsys.readouterr()
    assert "INSUIVABLES" in sortie.err and "Muet" in sortie.err


def test_ancienne_forme_a_deux_index_toujours_acceptee():
    """Un correctif qui casse la suite de tests existante ne se deploie
    jamais : `_tracke` doit tolerer l'ancien couple (wallet, user)."""
    fiche = {"username": "n", "type": "t"}
    assert ww._tracke(({"0xaa": fiche}, {}), "0xAA", None) is fiche
    assert ww._tracke(({}, {"bob": fiche}), None, "Bob") is fiche


# ═══════════════════════════════════════════════════════════════════════════
# 5. LE PONT — ce que l'export doit transporter
# ═══════════════════════════════════════════════════════════════════════════

def test_le_flux_veve_est_lu_sans_une_requete_de_plus():
    """`fetch_history` pagine deja ce flux chaque heure pour l'historique des
    ventes. On y branche un rappel plutot que de le paginer une 2e fois."""
    import inspect
    assert "sur_page" in inspect.signature(fw.fetch_history).parameters
    src = inspect.getsource(fw.main)
    assert "detect_veve" in src and "sur_page=" in src
