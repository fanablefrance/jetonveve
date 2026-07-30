# -*- coding: utf-8 -*-
"""A4 bis — la sentinelle ne doit pas confondre « je ne sais pas » et « ca va ».

    pytest tests/test_sentinelle_anglemort.py

CE QUI EST TESTE, ET POURQUOI. Mesure du 30/07/2026 sur `daily` #122 : le releve
imprimait `tracker 13 requete(s) — < 30 obs : verdict suspendu` ET
`tracker 114 requete(s) — RAS` DANS LE MEME RUN. Deux verdicts opposes pour la
meme source ne peuvent pas venir d'un compteur qui mûrit : `self.obs` vit en
memoire, un objet par processus, rien ne le persiste. `MIN_OBS` compte donc les
requetes DE CE RUN.

Consequence mesuree : `stackr` fait 6 requetes par run. Il n'atteindra JAMAIS 30.
Avant ce correctif, `verdict()` rendait « ouverte » sous le seuil et
`doit_crier()` ne crie que sur « se_ferme » : StackR pouvait refuser ses 6
requetes sur 6 et rester 🟢, indefiniment. C'est-a-dire exactement le defaut que
le module dit combattre — un canal muet lu comme un marche calme — reproduit a
l'interieur du garde-fou.

⭐ Ce banc DOIT etre rouge sur la version d'avant le 30/07 : sinon il ne mesure
rien. Les deux derniers cas sont des TEMOINS, verts des deux cotes.
"""
import pathlib
import sys

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[1]
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

from scraper import sentinelle_sources as ss   # noqa: E402


def _sentinelle(**reponses):
    """Fabrique une sentinelle et lui fait noter des codes HTTP.
    `_sentinelle(stackr=[403]*6)` = 6 refus sur la source `stackr`."""
    s = ss.Sentinelle()
    for source, codes in reponses.items():
        for c in codes:
            s.noter(source, code=c)
    return s


# --- 1. LE DEFAUT : peu d'observations ne veut pas dire « tout va bien » -----

def test_peu_d_observations_n_est_pas_un_feu_vert():
    """6 requetes OK : on ne peut rien conclure, mais surtout pas « ouverte »."""
    s = _sentinelle(stackr=[200] * 6)
    assert s.verdict("stackr") == "angle_mort", (
        "sous MIN_OBS le verdict rendait « ouverte » : l'ignorance se lisait "
        "comme une bonne nouvelle, et c'est ainsi qu'une source muette passe "
        "pour un marche calme.")


def test_le_releve_ne_dit_jamais_RAS_sur_un_angle_mort():
    s = _sentinelle(stackr=[200] * 6)
    txt = s.resume()
    assert "ANGLE MORT" in txt, txt
    assert "⚪" in txt, txt


# --- 2. La regle qui n'a pas besoin d'echantillon ---------------------------

def test_un_refus_TOTAL_crie_meme_sous_le_seuil():
    """6 refus sur 6 : c'est un fait, pas un taux. StackR bloque doit crier."""
    s = _sentinelle(stackr=[403] * 6)
    assert s.verdict("stackr") == "se_ferme", (
        "une source qui refuse TOUT restait 🟢 parce qu'elle n'atteignait pas "
        "30 observations — le cas exact de stackr, 6 requetes par run.")
    crier, msg = s.doit_crier()
    assert crier is True
    assert "stackr" in msg


def test_un_refus_total_ralentit_aussi():
    """Avant : aucune pause sous MIN_OBS, donc on martelait une source fermee."""
    s = _sentinelle(stackr=[403] * 6)
    assert s.pause_conseillee("stackr") > 0


def test_trois_refus_ne_suffisent_PAS():
    """⭐ Garde-fou du garde-fou, et il honore une lecon PAYEE : un banc existant
    (`test_sous_le_minimum_d_observations`) fige « 3 requetes refusees ne valent
    pas un verdict, on a deja paye trois fausses alertes en un jour ». C'est
    pourquoi REFUS_ABSOLU vaut 5 et non 3."""
    s = _sentinelle(stackr=[429, 429, 429])
    assert s.verdict("stackr") == "angle_mort"
    assert s.doit_crier()[0] is False
    assert s.pause_conseillee("stackr") == 0.0


def test_un_seul_succes_suffit_a_retomber_en_angle_mort():
    """La condition n'est pas « beaucoup de refus » mais « QUE des refus »."""
    s = _sentinelle(stackr=[403] * 5 + [200])
    assert s.verdict("stackr") == "angle_mort"
    assert s.doit_crier()[0] is False


def test_un_refus_PARTIEL_sous_le_seuil_reste_un_angle_mort():
    """On ne pretend pas avoir tout resolu : 3 refus sur 6 ne se juge pas.
    ⭐ Le releve le DIT, au lieu d'afficher un feu vert."""
    s = _sentinelle(stackr=[403, 403, 403, 200, 200, 200])
    assert s.verdict("stackr") == "angle_mort"
    assert "ANGLE MORT" in s.resume()


# --- 3. TEMOINS : le comportement au-dela du seuil ne change pas ------------

def test_temoin_source_saine_au_dela_du_seuil():
    s = _sentinelle(tracker=[200] * 40)
    assert s.verdict("tracker") == "ouverte"
    assert s.doit_crier()[0] is False
    assert "RAS" in s.resume()


def test_temoin_source_qui_se_ferme_au_dela_du_seuil():
    s = _sentinelle(tracker=[429] * 5 + [200] * 35)
    assert s.verdict("tracker") == "se_ferme"
    assert s.doit_crier()[0] is True


def test_temoin_aucune_requete():
    s = ss.Sentinelle()
    assert s.doit_crier()[0] is False
    assert "aucune requete" in s.resume()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
