# ⚠️ DEPOT : VeVePreda/scrapeur-veve   ·   CHEMIN : tests/test_sentinelle_partagee.py
"""A4 — la sentinelle etendue aux sources irremplacables (29/07/2026).

⛔⛔ CE BANC GARDE UN FICHIER QUI VIT EN DOUBLE.
`scraper/sentinelle_sources.py` existe a l'identique dans `fanablefrance/jetonveve`
ET `VeVePreda/scrapeur-veve`. L'audit des depots du 29/07 a nomme le module en
double qui a DIVERGE comme le vrai risque du projet — pas le fichier egare :
deux copies partent identiques, l'une recoit un correctif, l'autre continue de
tourner avec l'ancien comportement, et rien n'echoue.

Ce banc ne peut pas comparer les deux depots (ils ne se voient pas). Il fait la
seule chose faisable : rendre toute modification VISIBLE. Si tu touches ce
fichier, ce test tombe — et son message te rappelle de le porter des deux cotes.
"""
import hashlib
import pathlib

RACINE = pathlib.Path(__file__).resolve().parents[1]
MODULE = RACINE / "scraper" / "sentinelle_sources.py"

# Empreinte du fichier partage. ⚠️ LA DATE SE MET A JOUR AVEC L'EMPREINTE :
# le 06/08/2026 elle disait encore « au 29/07 » alors que le fichier avait
# bouge au lot 76 — un commentaire perime a cote d'un chiffre juste se relit
# comme une preuve que rien n'a change. Derniere mise a jour : 06/08/2026,
# lot 81 (le seau `absent` : HTTP 200 + errors[]).
EMPREINTE = "7da777643e5fe3cefafe54e7b606ec7c8468de17b931fe56d45e76ad68637315"


def test_le_module_partage_n_a_pas_bouge():
    vu = hashlib.sha256(MODULE.read_bytes()).hexdigest()
    assert vu == EMPREINTE, (
        "scraper/sentinelle_sources.py a change.\n"
        "⛔ Ce fichier est PARTAGE avec l'autre depot (jetonveve <-> scrapeur-veve).\n"
        "   1. porte la MEME modification dans l'autre depot ;\n"
        "   2. mets a jour EMPREINTE dans les DEUX tests :\n"
        f"      EMPREINTE = \"{vu}\"\n"
        "Si tu ne fais que le 2, les deux copies divergent en silence — et une\n"
        "sentinelle qui compte differemment des deux cotes ne vaut rien.")


def test_l_interface_publique_existe():
    """Un renommage silencieux casserait le cablage des collecteurs sans que
    rien n'echoue a l'import (les appels sont dans des `try`)."""
    from scraper import sentinelle_sources as ss
    for nom in ("Sentinelle", "SENTINELLE", "noter_reponse", "resume", "doit_crier"):
        assert hasattr(ss, nom), nom


def test_un_refus_et_une_panne_reseau_ne_se_comptent_pas_pareil():
    """⭐ La distinction pour laquelle ce module existe : 429 = « on nous
    repousse », 5xx/timeout = « la source est en peine ». Les deux mouraient
    ensemble dans le `except Exception` des collecteurs."""
    from scraper import sentinelle_sources as ss

    class Reponse:
        def __init__(self, c):
            self.status_code = c

    s = ss.Sentinelle()
    for _ in range(40):
        s.noter("src", 429)
    assert s.verdict("src") == "se_ferme"

    s2 = ss.Sentinelle()
    for _ in range(40):
        s2.noter("src", 503)
    assert s2.verdict("src") == "lente", "un 5xx n'est PAS un bannissement"

    s3 = ss.Sentinelle()
    for _ in range(40):
        s3.noter("src", None, "timeout")
    assert s3.verdict("src") == "lente"


def test_sous_le_minimum_d_observations_aucun_verdict():
    """Un pourcentage sur 3 requetes ne veut rien dire. Sans ce plancher, la
    sentinelle deviendrait elle-meme une source de fausses alertes — on en a
    deja paye trois en un jour."""
    from scraper import sentinelle_sources as ss
    s = ss.Sentinelle()
    for _ in range(3):
        s.noter("src", 429)
    # A4 bis : 3 refus ne valent toujours PAS un verdict — la lecon est
    # intacte, REFUS_ABSOLU vaut 5 pour cela. Seule l'etiquette change :
    # « angle_mort » dit qu'on ne sait pas, « ouverte » disait que ca allait.
    assert s.verdict("src") == "angle_mort"
    crier, _ = s.doit_crier()
    assert crier is False


def test_noter_reponse_lit_le_statut_sans_importer_requests():
    """`noter_reponse` est duck-type exprès : le module ne doit JAMAIS importer
    `requests`, sinon il cesse d'etre testable hors reseau."""
    from scraper import sentinelle_sources as ss
    assert "import requests" not in MODULE.read_text(encoding="utf-8")

    class FausseReponse:
        status_code = 403

    s_avant = dict(ss.SENTINELLE.obs.get("_banc", {}))
    ss.noter_reponse("_banc", FausseReponse())
    d = ss.SENTINELLE.obs["_banc"]
    assert d["repousse"] == s_avant.get("repousse", 0) + 1
