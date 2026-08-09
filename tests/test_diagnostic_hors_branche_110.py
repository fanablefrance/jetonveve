# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_diagnostic_hors_branche_110.py
"""LOT 110 — le diagnostic compte MEME quand le rafraichissement horaire
n'a pas eu lieu.

🔴🔴🔴 CE QUE CE BANC PROTEGE.

Le lot 109 a mis le diagnostic dans l'etat, et son PREMIER run reel a signale
son propre angle mort : `comptes_suivis: None`. `journal_identite()` etait
appelee TROIS branches trop bas — dans la boucle des tours, dans le bloc du
rafraichissement horaire, et sous `whale_actif and _wveve`. Or un run fait
25 tours x 120 s ~= 50 min pour un refresh a 60 min : **la plupart des runs ne
croisent jamais ce bloc**. Le champ le plus utile du rapport etait absent la
plupart du temps, et `wallets_sondes` avec lui — donc 🔀 restait non tranche.

⭐⭐⭐ ET C'EST LE DESIGN QUI L'A ATTRAPE. `None` veut dire « on n'a pas
cherche », et c'est exactement ce qui se passait. Initialise a `0`, le champ
aurait annonce « 0 compte suivi » — un mensonge PLAUSIBLE, et on serait parti
fouiller le Sheet. *Distinguer « inconnu » de « zero » a trouve un defaut de
cablage au premier run reel.*

⛔ LE PIEGE QUE CE BANC GARDE SURTOUT. La correction evidente etait de poser
`wallets_sondes = len(wtracked[0])` des le chargement. Elle marche, elle ne
casse rien — et elle CHANGE LE SENS DU CHAMP : « sondes » deviendrait
« connus ». Un `7` serait alors lu « ces comptes ne font pas de gros
transferts » alors que le detecteur n'aurait pas tourne. **Le meme nombre,
deux causes opposees.** ⇒ deux champs, trois verdicts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import floor_watch as fw          # noqa: E402

MAINTENANT = 1786117797.0


def _corps_de_main() -> str:
    src = open(fw.__file__, encoding="utf-8").read()
    return src[src.index("\ndef main("):]


# ── ① L'APPEL EST SORTI DE SES TROIS BRANCHES ───────────────────────────
def test_journal_identite_est_appelee_APRES_la_boucle_des_tours():
    """🔴 LE BANC CENTRAL. La boucle se termine sur `Termine : ... tours`.
    Tout appel situe AVANT ce point est, par construction, dans la boucle —
    donc conditionne au tour ou l'on tombe.

    ⚠️ Ce controle lit le SOURCE, faute de pouvoir jouer `main()` hors reseau.
    C'est la meme faiblesse assumee que `test_main_SAUVE_...` du lot 109 : il
    casse si on renomme la marque. Il vaut mieux qu'un trou.
    """
    corps = _corps_de_main()
    fin_de_boucle = corps.index('print(f"Termine : {POLLS} tours')
    appels = [i for i in range(len(corps))
              if corps.startswith("ww.journal_identite(", i)]
    assert appels, "main() n'appelle plus le journal d'identite du tout"
    assert len(appels) == 1, (
        "deux appels = deux sources de verite, et un double affichage. "
        "Le lot 110 a DEPLACE l'appel, il ne l'a pas duplique.")
    assert appels[0] > fin_de_boucle, (
        "🔴 l'appel est reste DANS la boucle des tours : il ne s'executera "
        "que si le run croise le bloc qui le porte — c'est exactement le "
        "defaut du 09/08.")


def test_le_comptage_des_wallets_connus_est_pose_AVANT_la_boucle():
    """⭐ `_diag_connus` compte ce qu'on vient de charger. Il ne depend
    d'aucun reseau, d'aucun tour, d'aucun refresh — donc il n'a rien a faire
    dans une branche."""
    corps = _corps_de_main()
    i = corps.index("_diag_connus =")
    debut_de_boucle = corps.index("if whale_actif:")
    assert i < debut_de_boucle, (
        "le comptage des wallets connus est descendu dans une branche")
    ligne = corps[corps.rindex("\n", 0, i) + 1:corps.index("\n", i)]
    assert len(ligne) - len(ligne.lstrip()) == 4, (
        "il doit etre au premier niveau de main(), pas indente dans un bloc")


# ── ② TROIS VERDICTS POUR 🔀, JAMAIS DEUX ───────────────────────────────
def test_wallets_connus_None_nest_ni_zero_ni_sonde():
    """⚠️ None = module whale eteint · 0 = allume, aucun wallet renseigne."""
    assert fw.diagnostic({}, maintenant=MAINTENANT)["wallets_connus"] is None
    d = fw.diagnostic({}, None, None, maintenant=MAINTENANT, wallets_connus=0)
    assert d["wallets_connus"] == 0


def test_connus_et_sondes_sont_DEUX_champs_distincts():
    """🔴 LE BANC QUI GARDE LA CORRECTION EVIDENTE ET FAUSSE.

    Si un lot futur fait pointer les deux sur le meme calcul, ce banc rougit.
    Le cas ci-dessous est le cas REEL du 09/08 : 7 wallets connus, le
    detecteur n'a pas tourne.
    """
    d = fw.diagnostic({}, None, None, maintenant=MAINTENANT, wallets_connus=7)
    assert d["wallets_connus"] == 7
    assert d["wallets_sondes"] is None, (
        "⛔ « connus » vient de se faire passer pour « sondes » : un 7 sera "
        "lu « ces comptes sont calmes » alors que rien n'a ete sonde")


def test_le_verdict_imprime_dit_INDECIDABLE_et_pas_rien_a_signaler(capsys):
    """⭐⭐⭐ « Je ne sais pas » ne doit JAMAIS emprunter la sortie de « rien
    a signaler ». C'est la regle des trois verdicts du lot 109, appliquee au
    canal 🔀."""
    fw.imprimer_diagnostic(
        fw.diagnostic({}, None, None, maintenant=MAINTENANT, wallets_connus=7))
    sortie = capsys.readouterr().out
    assert "indecidable" in sortie
    assert "calmes" not in sortie


def test_le_verdict_imprime_distingue_aucun_wallet_de_wallets_calmes(capsys):
    fw.imprimer_diagnostic(
        fw.diagnostic({}, None, 0, maintenant=MAINTENANT, wallets_connus=0))
    aucun = capsys.readouterr().out
    assert "aucun wallet connu" in aucun and "C-PSEUDOS" in aucun

    fw.imprimer_diagnostic(
        fw.diagnostic({}, None, 7, maintenant=MAINTENANT, wallets_connus=7))
    calmes = capsys.readouterr().out
    assert "7/7" in calmes and "calmes" in calmes
    assert "indecidable" not in calmes


def test_whale_eteint_ne_dit_RIEN_du_canal_transferts(capsys):
    """⛔ Un module eteint n'a pas d'avis. Imprimer « aucun wallet connu »
    quand le module est OFF enverrait completer un Sheet pour rien."""
    fw.imprimer_diagnostic(fw.diagnostic({}, maintenant=MAINTENANT))
    assert "🔀" not in capsys.readouterr().out


# ── ③ CE QUE LE CHAMP SERT A TRANCHER ───────────────────────────────────
def test_les_comptages_survivent_a_lecriture(tmp_path, monkeypatch):
    """⭐ Un releve qui n'atteint pas son lecteur ne sert a rien — et le
    lecteur, ici, est une Release publique relue des semaines plus tard."""
    import json
    f = tmp_path / "etat.json"
    monkeypatch.setattr(fw, "STATE_PATH", str(f))
    st = {}
    fw.diagnostic(st, {"total": 7, "ont_declenche": 6, "sans_cle": 0,
                       "sans_wallet": 2, "jamais": 1}, None,
                  maintenant=MAINTENANT, wallets_connus=7)
    fw.save_state(st)
    relu = json.loads(f.read_text(encoding="utf-8"))["diagnostic"]
    assert relu["comptes_suivis"]["total"] == 7
    assert relu["wallets_connus"] == 7 and relu["wallets_sondes"] is None


def test_toujours_aucun_pseudo_ni_adresse_dans_letat():
    """⛔ La Release est PUBLIQUE. Le lot 110 ajoute un champ : il doit passer
    la meme regle que le 109. On compte, on ne nomme pas."""
    import json
    from scraper import whale_watch as ww
    fiche = {"username": "Granolawarfare", "type": "whale",
             "wallet_imx": "0x" + "ab" * 20, "wallet_stackr": "",
             "veve_user_id": "556d60f1-efda", "holdings": "", "value_floor": ""}
    tracked = ({fiche["wallet_imx"]: fiche}, {"g": fiche}, {})
    st = {}
    d = fw.diagnostic(st, ww.journal_identite(st, tracked), None,
                      maintenant=MAINTENANT, wallets_connus=1)
    brut = json.dumps(d, ensure_ascii=False)
    for interdit in ("Granolawarfare", "0xab", "556d60f1"):
        assert interdit not in brut, f"⛔ {interdit} ne doit pas etre publie"
