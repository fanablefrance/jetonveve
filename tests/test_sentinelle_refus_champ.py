# -*- coding: utf-8 -*-
# ⚠️ DEPOT : fanablefrance/jetonveve  ET  VeVePreda/scrapeur-veve
# ⚠️ CHEMIN : tests/test_sentinelle_refus_champ.py  (le meme dans les deux)
#
# ⛔⛔ Comme le module qu'il surveille, ce banc vit EN DOUBLE, A L'IDENTIQUE.
# Le deposer d'un seul cote recreerait la divergence que `test_sentinelle_
# partagee.py` existe pour interdire.

"""🔧 LE REFUS DE *NOTRE REQUETE* — celui que la sentinelle ne voyait pas.

⭐⭐ CE QUE CE BANC ATTRAPE : un run qui essuie des HTTP 400 et se declare
« RAS ». Pas une panne, pas une exception : une ligne de log rassurante.

L'HISTOIRE, mesuree le 05/08/2026. Le run `ENRICH_MODE=all` a essuye des
HTTP 400 sur `publicComicType` (des champs sondes qui n'existent pas). Verdict
imprime en fin de run :

    🟢 veve_graphql    7130 requete(s)  — RAS

Le compteur faisait litteralement `d["serveur"] += 0` et `d["ok"] += 0`, avec
le commentaire « ni bon ni mauvais signe : on ne compte que dans le total ».
⭐⭐⭐ **UN COMPTEUR QUI N'INCREMENTE RIEN N'EST PAS NEUTRE : IL EST MUET.**

⛔ ET LA CORRECTION NE CONSISTE PAS A LES COMPTER COMME DES REFUS. Un 429 dit
« la source nous REPOUSSE » et se soigne en ralentissant ; un 400 dit « NOTRE
REQUETE est fausse » et se soigne en corrigeant le code. Les additionner ferait
ralentir un run qui n'a aucun probleme de debit — et ferait disparaitre le vrai
defaut derriere une fausse cause. Les tests ci-dessous verrouillent **les
deux** : qu'on les VOIE, et qu'on ne les CONFONDE pas.

    python3 -m pytest tests/test_sentinelle_refus_champ.py -q
"""

import pytest

from scraper.sentinelle_sources import Sentinelle


def _sent(codes):
    s = Sentinelle()
    for c in codes:
        s.noter("veve_graphql", code=c)
    return s


# ---------------------------------------------------------------------------
# 🔴 LE VERROU CENTRAL — « RAS » doit devenir impossible
# ---------------------------------------------------------------------------
def test_un_400_ne_peut_plus_se_lire_RAS():
    """Le cas EXACT du 05/08 : beaucoup de succes, quelques 400."""
    s = _sent([200] * 7127 + [400, 400, 400])
    txt = s.resume()
    assert "RAS" not in txt, (
        "la sentinelle affiche encore « RAS » alors que 3 requêtes ont été "
        "refusées — c'est le défaut que ce banc existe pour interdire")
    assert "3" in txt


def test_le_message_dit_QUI_est_en_cause():
    """⭐ Un compteur qui dit « 3 anomalies » n'aide personne. Celui-ci doit
    dire que c'est NOTRE requête, sinon le lecteur cherchera du côté de la
    source — et ralentira, ce qui ne changera rien."""
    txt = _sent([200] * 100 + [400]).resume()
    assert "NOTRE requete" in txt or "NOTRE requête" in txt
    assert "REQUETE INVALIDE" in txt or "REFUSEE" in txt
    assert "Ralentir n'y changera rien" in txt


def test_les_400_sont_comptes():
    s = _sent([200] * 10 + [400, 404, 422])
    d = s.obs["veve_graphql"]
    assert d["invalide"] == 3
    assert d["total"] == 13


# ---------------------------------------------------------------------------
# ⛔ ET SURTOUT : NE PAS LES CONFONDRE AVEC UN REFUS DE LA SOURCE
# ---------------------------------------------------------------------------
def test_un_400_n_est_PAS_un_refus_de_la_source():
    """🔴 Si les 400 comptaient comme `repousse`, un run entier de requêtes
    malformées déclencherait « la source nous repousse », une pause, et une
    alerte Discord — pour un problème que ralentir n'atténue pas."""
    s = _sent([400] * 100)
    d = s.obs["veve_graphql"]
    assert d["repousse"] == 0
    assert s.verdict("veve_graphql") != "se_ferme"
    assert s.pause_conseillee("veve_graphql") == 0.0
    assert s.doit_crier()[0] is False


def test_un_429_reste_un_refus_de_la_source():
    """⭐ LE TÉMOIN SAIN. Sans lui, un banc qui neutralise tout passerait pour
    rigoureux — c'est la leçon du 30/07 sur `test:dockerfile`."""
    s = _sent([429] * 100)
    assert s.obs["veve_graphql"]["repousse"] == 100
    assert s.verdict("veve_graphql") == "se_ferme"
    assert s.pause_conseillee("veve_graphql") > 0
    assert s.doit_crier()[0] is True


def test_un_5xx_reste_une_source_en_peine():
    s = _sent([200] * 60 + [503] * 40)
    assert s.obs["veve_graphql"]["serveur"] == 40
    assert s.obs["veve_graphql"]["invalide"] == 0
    assert s.verdict("veve_graphql") == "lente"


# ---------------------------------------------------------------------------
# LE CAS LE PLUS SOURNOIS : 100 % de requêtes invalides
# ---------------------------------------------------------------------------
def test_tout_refuse_pour_requete_invalide_reste_VISIBLE():
    """⭐⭐ La source est parfaitement « ouverte » — et elle a raison, c'est
    nous qui demandons mal. Le verdict porte sur ELLE, donc il reste vert.
    C'est exactement pour ça que la ligne 🔧 existe : *le bon verdict sur la
    mauvaise question rassure autant qu'un faux verdict.*"""
    s = _sent([400] * 200)
    txt = s.resume()
    assert s.verdict("veve_graphql") == "ouverte"     # vrai, et insuffisant
    assert "200" in txt and "🔧" in txt
    assert "RAS" not in txt


def test_une_source_saine_reste_silencieuse():
    """⛔ Pas de bruit sur le cas normal. *Un avertissement qui se déclenche
    sur le cas normal est du bruit, et le bruit se lit comme du silence.*"""
    txt = _sent([200] * 200).resume()
    assert "RAS" in txt
    assert "🔧" not in txt
