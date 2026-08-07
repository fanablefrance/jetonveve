# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_diagnostic_0708.py
"""LOT 109 — le diagnostic des canaux d'alerte, ecrit dans l'etat.

🔴🔴🔴 CE QUE CE BANC PROTEGE, ET POURQUOI IL EXISTE.

Le 07/08/2026, la memoire du projet portait « ALERTES FLOOR, 4 muettes sur 7 —
LE produit ». Pour savoir si c'etait vrai il a fallu telecharger l'etat depuis
une Release, DEVINER la structure de chaque cle et reconstruire les dates a
l'heuristique. Verdict reel : **rien n'etait casse** — 6 comptes sur 7 avaient
declenche, et les canaux « silencieux » s'etaient tus le jour ou Preda les
avait eteints.

⭐⭐⭐ LA RECONSTRUCTION DEPUIS L'EXTERIEUR EST FAUSSE PAR CONSTRUCTION :
`comics_veve_dedans` a exactement la meme forme que les index d'alertes, mais
ses valeurs sont des PRIX. Un lecteur du dehors lit « aucune date » et comprend
« ce canal n'a jamais tire ». Deux cas qui se ressemblent et qui sont l'inverse.

Ce banc verifie les quatre proprietes qui rendent le diagnostic FIABLE :
  ① il distingue les TROIS verdicts (a tire / jamais / indecidable) ;
  ② il declare si un canal est ALLUME (« zero casse » != « zero eteint ») ;
  ③ il ne NOMME personne (l'etat est publie en Release PUBLIQUE) ;
  ④ il est SAUVE (un releve qui n'atteint pas son lecteur ne sert a rien).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import floor_watch as fw          # noqa: E402

MAINTENANT = 1786117797.0                      # fige : 07/08/2026 ~15:49 UTC
H = 3600.0


# ── ① LES TROIS VERDICTS ────────────────────────────────────────────────
def test_un_canal_qui_a_tire_porte_sa_date():
    st = {"mints_vus": {"a": MAINTENANT - 3 * H}}
    d = fw.diagnostic(st, maintenant=MAINTENANT)["canaux"]["mints_vus"]
    assert d["dernier"] == MAINTENANT - 3 * H
    assert d["age_h"] == 3.0
    assert d["horodate"] is True


def test_un_canal_vide_dit_JAMAIS_pas_INDECIDABLE():
    """⭐ Vide n'est pas ambigu : la cle est connue, elle n'a rien recu."""
    d = fw.diagnostic({"alerts_pic": {}}, maintenant=MAINTENANT)["canaux"]
    assert d["alerts_pic"]["dernier"] is None
    assert d["alerts_pic"]["horodate"] is True, (
        "un canal vide a bien JAMAIS tire — ce n'est pas un doute")


def test_un_index_qui_porte_des_PRIX_est_declare_INDECIDABLE():
    """🔴 LE BANC CENTRAL. `comics_veve_dedans` stocke des prix (1.95).

    Sans `horodate: False`, il serait rendu « dernier: null » — indiscernable
    d'un canal qui n'a jamais tire, et on partirait reparer ce qui marche.
    ⭐ « Je ne sais pas » ne doit JAMAIS emprunter la sortie de « rien a
    signaler ».
    """
    st = {"comics_veve_dedans": {"uuid": 1.95, "autre": 12.0}}
    d = fw.diagnostic(st, maintenant=MAINTENANT)["canaux"]["comics_veve_dedans"]
    assert d["entrees"] == 2, "les entrees sont bien comptees"
    assert d["dernier"] is None
    assert d["horodate"] is False, (
        "un index sans horodatage doit se DECLARER indecidable, "
        "jamais se faire passer pour un canal muet")


def test_le_SEUIL_est_le_seul_garde_fou_et_il_reste_haut():
    """🔴🔴 CE BANC A CHANGE DE NOM APRES UN DESARMEMENT, ET C'EST LA LEÇON.

    Sa v1 s'appelait « un booleen n'est jamais pris pour une date » et le code
    portait `not isinstance(v, bool)`. **Desarme, ce banc restait VERT** :
    `True` vaut 1, il ne franchit jamais le seuil. La ligne ne gardait rien —
    une regle sans emetteur, qui avait l'air d'un soin et masquait la vraie
    protection. Elle a ete retiree.
    ⭐⭐ Un banc vert ne prouve rien tant qu'on ne l'a pas vu ROUGIR sur le
    retrait de ce qu'il pretend garder.

    Ce qui protege reellement, c'est `SEUIL_EPOCH`. Donc c'est LUI qu'on
    verifie — sur son role (ecarter prix, comptages, editions, booleens) ET
    sur sa valeur (le baisser rouvrirait la porte).
    """
    assert fw.SEUIL_EPOCH >= 1_000_000_000, (
        "un seuil bas laisserait passer prix, comptages et numeros d'edition")
    for valeur, quoi in ((True, "un booleen"), (1.95, "un prix"),
                         (600, "un numero d'edition"), (0, "un zero")):
        d = fw.diagnostic({"alerts_vol": {"x": valeur}},
                          maintenant=MAINTENANT)["canaux"]["alerts_vol"]
        assert d["dernier"] is None, f"{quoi} pris pour une date"
        assert d["horodate"] is False


# ── ② LE DRAPEAU ON/OFF ─────────────────────────────────────────────────
def test_chaque_canal_declare_sil_est_allume(monkeypatch):
    """⭐⭐ « zero parce que casse » et « zero parce qu'ETEINT » se
    ressemblent sur le disque et sont l'inverse. Le releve sort sur une
    DECLARATION, jamais sur un comptage seul."""
    monkeypatch.setattr(fw, "ATL_ON", False)
    monkeypatch.setattr(fw, "MINT_ON", True)
    c = fw.diagnostic({}, maintenant=MAINTENANT)["canaux"]
    assert c["alerts_atl"]["actif"] is False
    assert c["mints_vus"]["actif"] is True
    assert all("actif" in v for v in c.values()), "AUCUN canal sans drapeau"


def test_le_releve_imprime_ne_tranche_pas(capsys, monkeypatch):
    """⭐ Un canal ALLUME et sans tir depuis 7 j est SIGNALE, mais le texte
    dit explicitement qu'il ne conclut pas. C'est ce qui manquait : « 4
    muettes » a ete lu comme un verdict alors que c'etait une observation."""
    monkeypatch.setattr(fw, "PIC_ON", True)
    d = fw.diagnostic({"alerts_pic": {"a": MAINTENANT - 500 * H}},
                      maintenant=MAINTENANT)
    fw.imprimer_diagnostic(d)
    sortie = capsys.readouterr().out
    assert "pic hors drop" in sortie
    assert "ne tranche pas" in sortie, (
        "le releve doit dire qu'il ne tranche pas — sinon il fabrique "
        "un defaut la ou il n'y a peut-etre qu'un canal rare")


# ── ③ ON COMPTE, ON NE NOMME PAS ────────────────────────────────────────
def test_aucun_pseudo_ni_wallet_dans_letat(monkeypatch):
    """⛔ `floor_state.json` est publie en Release PUBLIQUE. La liste des
    comptes surveilles est une strategie de veille (arbitrage Preda, 07/08).

    ⭐⭐ Le banc lit le JSON SERIALISE, pas la structure : une regle verifiee
    sur la sortie ne se contourne pas par un chemin qu'on n'avait pas prevu.
    """
    from scraper import whale_watch as ww
    fiche = {"username": "Granolawarfare", "type": "whale",
             "wallet_imx": "0x" + "ab" * 20, "wallet_stackr": "",
             "veve_user_id": "556d60f1-efda-4939-9c93-5bcfd1e570ff",
             "holdings": "", "value_floor": ""}
    tracked = ({fiche["wallet_imx"]: fiche}, {"granolawarfare": fiche},
               {fiche["veve_user_id"]: fiche})
    st = {}
    comptes = ww.journal_identite(st, tracked)
    d = fw.diagnostic(st, comptes, 1, maintenant=MAINTENANT)

    brut = json.dumps(d, ensure_ascii=False)
    assert "Granolawarfare" not in brut, "⛔ aucun pseudo dans l'etat public"
    assert "0xab" not in brut, "⛔ aucune adresse dans l'etat public"
    assert "556d60f1" not in brut, "⛔ aucun veve_user_id dans l'etat public"
    assert d["comptes_suivis"]["total"] == 1
    assert d["comptes_suivis"]["ont_declenche"] == 0


def test_journal_identite_rend_les_memes_chiffres_quil_imprime(capsys):
    """⭐ UNE SOURCE, PAS DEUX. Le texte du log et les nombres de l'etat
    viennent du meme calcul — « deux parseurs, c'est un qui ment »."""
    from scraper import whale_watch as ww
    f1 = {"username": "A", "wallet_imx": "0x" + "1" * 40, "wallet_stackr": "",
          "veve_user_id": "u1", "type": "whale", "holdings": "",
          "value_floor": ""}
    f2 = {"username": "B", "wallet_imx": "", "wallet_stackr": "",
          "veve_user_id": "u2", "type": "team", "holdings": "",
          "value_floor": ""}
    tracked = ({f1["wallet_imx"]: f1}, {"a": f1, "b": f2},
               {"u1": f1, "u2": f2})
    st = {"whale_comptes_vus": {"A": MAINTENANT}}
    c = ww.journal_identite(st, tracked)
    sortie = capsys.readouterr().out
    assert c == {"total": 2, "ont_declenche": 1, "sans_cle": 0,
                 "sans_wallet": 1, "jamais": 1}
    assert "2 · 1 ont deja declenche" in sortie


def test_wallets_sondes_None_nest_pas_zero():
    """⚠️ None = « on n'a pas cherche » · 0 = « on a cherche, rien trouve ».
    Les deux se ressemblent dans un rapport et appellent l'inverse."""
    assert fw.diagnostic({}, maintenant=MAINTENANT)["wallets_sondes"] is None
    assert fw.diagnostic({}, None, 0,
                         maintenant=MAINTENANT)["wallets_sondes"] == 0


# ── ④ IL DOIT ETRE SAUVE ────────────────────────────────────────────────
def test_le_diagnostic_est_ECRIT_dans_letat(tmp_path, monkeypatch):
    """🔴 `save_state` vit DANS la boucle des tours ; le diagnostic est
    calcule APRES. Sans son propre `save_state`, il serait parfait et
    n'atteindrait jamais son lecteur — le defaut exact que ce lot corrige.
    Ce banc relit le FICHIER, pas la variable."""
    f = tmp_path / "etat.json"
    monkeypatch.setattr(fw, "STATE_PATH", str(f))
    st = {"mints_vus": {"a": MAINTENANT - H}}
    fw.diagnostic(st, {"total": 7, "ont_declenche": 6, "sans_cle": 0,
                       "sans_wallet": 2, "jamais": 1}, 1, maintenant=MAINTENANT)
    fw.save_state(st)
    relu = json.loads(f.read_text(encoding="utf-8"))
    assert "diagnostic" in relu, "le diagnostic doit survivre a l'ecriture"
    assert relu["diagnostic"]["comptes_suivis"]["ont_declenche"] == 6
    assert relu["diagnostic"]["canaux"]["mints_vus"]["age_h"] == 1.0


def test_main_SAUVE_apres_avoir_calcule_le_diagnostic():
    """🔴🔴 LE BANC PRECEDENT NE SUFFIT PAS, ET IL FAUT LE DIRE.

    Il appelle `save_state` LUI-MEME : il prouve que le diagnostic survit a une
    ecriture, pas que `main()` l'ecrit. Un lot qui retirerait le `save_state`
    final le laisserait VERT — et le diagnostic serait calcule a chaque run
    puis jete, ce qui est exactement le defaut corrige ici.
    ⭐⭐ *Un banc se juge sur ce qu'il LAISSE PASSER.*

    ⚠️ Ce controle-ci lit le SOURCE, faute de pouvoir jouer `main()` hors
    reseau. C'est une faiblesse assumee et ecrite : il casse si on renomme
    `save_state`. Il vaut mieux qu'un trou.
    """
    src = open(fw.__file__, encoding="utf-8").read()
    corps = src[src.index("def main("):]
    i = corps.index("_diag = diagnostic(")
    reste = corps[i:]
    j = reste.index("save_state(state)")
    entre = reste[:j]
    assert "def " not in entre, (
        "le save_state trouve appartient a une autre fonction — "
        "le diagnostic n'est donc PAS sauve par main()")


def test_le_diagnostic_est_remplace_jamais_empile():
    """⭐ L'etat pese deja 3,7 Mo. Un diagnostic qui s'empile le ferait
    grossir a chaque run — et un releve qui coute cher finit desactive."""
    st = {}
    fw.diagnostic(st, maintenant=MAINTENANT)
    fw.diagnostic(st, maintenant=MAINTENANT + 60)
    assert isinstance(st["diagnostic"], dict)
    assert st["diagnostic"]["ts"] == MAINTENANT + 60


def test_il_est_ecrit_meme_quand_rien_ne_sest_passe():
    """⭐ Sinon son absence ressemblerait a « tout va bien » — c'est la
    lecon des sentinelles, transposee ici."""
    d = fw.diagnostic({}, maintenant=MAINTENANT)
    assert d["canaux"] and d["iso"].endswith("Z")
    assert len(d["canaux"]) >= 14, "tous les canaux sont declares, meme vides"
