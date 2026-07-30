# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_sentinelle_sources.py
"""La sentinelle doit etre plus fiable que ce qu'elle surveille.

⭐⭐ POURQUOI CE FICHIER EST LONG POUR UN MODULE DE 130 LIGNES. On a deja paye
trois fausses alertes en une journee, toutes dues a NOS instruments et non aux
donnees. Un garde-fou qui se trompe coute plus cher que pas de garde-fou : on
regle dans le vide en croyant progresser. Une sentinelle se teste donc plus
severement que le code qu'elle protege.
"""

import pytest

from scraper import sentinelle_sources as ss


@pytest.fixture
def s():
    return ss.Sentinelle()


def _remplir(sent, source, n, code):
    for _ in range(n):
        sent.noter(source, code)


# ───────────────────── ne pas crier sur trois requetes ─────────────────────

def test_pas_de_verdict_sous_le_minimum_d_observations(s):
    """Le piege classique : 2 refus sur 3 requetes = 66 %, et pourtant on ne
    sait RIEN. Un pourcentage sans denominateur n'est pas une mesure.

    ⭐ A4 bis (30/07/2026) : l'INTENTION de ce test est « ne pas crier, ne pas
    ralentir » — les deux assertions du bas. Elle est INTACTE. Seule l'etiquette
    change : sous le seuil le verdict vaut desormais « angle_mort » et non
    « ouverte », parce que rendre « ouverte » faisait lire l'ignorance comme une
    bonne nouvelle. C'est le defaut que ce module combat, applique a lui-meme."""
    _remplir(s, "stackr", 2, 429)
    s.noter("stackr", 200)
    assert s.verdict("stackr") == "angle_mort"
    assert s.doit_crier()[0] is False
    assert s.pause_conseillee("stackr") == 0.0


def test_source_inconnue_est_un_angle_mort(s):
    """A4 bis : jamais interrogee = rien a dire. Aucun effet de bord (elle
    n'apparait pas dans le releve, qui n'itere que les sources observees)."""
    assert s.verdict("jamais_vue") == "angle_mort"
    assert s.pause_conseillee("jamais_vue") == 0.0


# ───────────────────────── les trois verdicts ─────────────────────────

def test_source_normale_reste_verte(s):
    _remplir(s, "stackr", 100, 200)
    assert s.verdict("stackr") == "ouverte"


def test_refus_repetes_donnent_se_ferme(s, monkeypatch):
    monkeypatch.setattr(ss, "MIN_OBS", 30)
    _remplir(s, "stackr", 90, 200)
    _remplir(s, "stackr", 10, 429)      # 10 % >= seuil 5 %
    assert s.verdict("stackr") == "se_ferme"


def test_un_403_compte_comme_un_refus(s):
    """403 = « pas vous ». C'est un refus, pas une panne."""
    _remplir(s, "stackr", 90, 200)
    _remplir(s, "stackr", 10, 403)
    assert s.verdict("stackr") == "se_ferme"


def test_5xx_massifs_donnent_lente_pas_se_ferme(s):
    """⭐ LA DISTINCTION QUI COMPTE. Une source en panne (5xx) et une source
    qui nous repousse (429) demandent des reactions OPPOSEES. Les confondre,
    c'est ralentir pour rien — ou ne pas ralentir quand il le faudrait."""
    _remplir(s, "stackr", 70, 200)
    _remplir(s, "stackr", 30, 503)
    assert s.verdict("stackr") == "lente"
    assert s.doit_crier()[0] is False, "un 5xx n'est pas notre faute : pas de cri"
    assert s.pause_conseillee("stackr") == 0.0, "ralentir n'aide pas un 5xx"


def test_les_echecs_reseau_comptent_comme_de_la_lenteur(s):
    _remplir(s, "stackr", 70, 200)
    for _ in range(30):
        s.noter("stackr", None, "timeout")
    assert s.verdict("stackr") == "lente"


# ─────────────────────── la pause, et son plafond ───────────────────────

def test_la_pause_grandit_avec_le_taux_de_refus(s):
    _remplir(s, "a", 95, 200); _remplir(s, "a", 5, 429)
    _remplir(s, "b", 70, 200); _remplir(s, "b", 30, 429)
    assert 0 < s.pause_conseillee("a") < s.pause_conseillee("b")


def test_la_pause_est_plafonnee(s):
    """Sans plafond, une source qui refuse tout ferait dormir le run entier."""
    _remplir(s, "stackr", 100, 429)
    assert s.pause_conseillee("stackr") <= ss.PAUSE_MAX_S


# ─────────────────────────── le cri ───────────────────────────

def test_le_cri_nomme_la_source_et_donne_les_chiffres(s):
    _remplir(s, "stackr", 90, 200)
    _remplir(s, "stackr", 10, 429)
    crier, texte = s.doit_crier()
    assert crier is True
    assert "stackr" in texte
    assert "10" in texte and "100" in texte
    assert "calme" in texte, (
        "le message doit dire qu'une source muette N'EST PAS un marche "
        "calme — c'est toute son utilite.")


def test_pas_de_cri_quand_tout_va_bien(s):
    _remplir(s, "stackr", 200, 200)
    assert s.doit_crier() == (False, "")


# ─────────────────────── le releve, et sa prudence ───────────────────────

def test_le_releve_dit_quand_il_ne_sait_pas(s):
    """Un releve qui affiche 🟢 sur 5 observations ment par omission."""
    _remplir(s, "stackr", 5, 200)
    # A4 bis : « verdict suspendu » se lisait « ca va, patience » alors que
    # rien ne mûrit — les observations ne s'accumulent pas entre les runs.
    assert "ANGLE MORT" in s.resume()


def test_le_releve_liste_chaque_source(s):
    _remplir(s, "stackr", 40, 200)
    _remplir(s, "collectscan", 40, 200)
    r = s.resume()
    assert "stackr" in r and "collectscan" in r


def test_releve_vide_ne_plante_pas(s):
    assert "aucune requete" in s.resume()


# ────────────────────── elle ne doit RIEN casser ──────────────────────

def test_la_sentinelle_ne_leve_jamais(s):
    """Elle est appelee dans un chemin d'erreur. Si elle levait, elle
    transformerait une requete ratee en run mort."""
    for mauvais in (None, "", 0, -1, 999, 3.5):
        s.noter("x", mauvais if isinstance(mauvais, (int, type(None))) else None)
    s.noter("", None)
    assert s.resume()


def test_le_module_ne_touche_pas_au_reseau():
    """⭐ Un garde-fou qui depend de ce qu'il surveille ne garde rien."""
    src = open(ss.__file__, encoding="utf-8").read()
    for interdit in ("import requests", "urllib", "socket", "open("):
        assert interdit not in src, (
            f"sentinelle_sources contient « {interdit} » : il doit rester "
            f"PUR (aucun reseau, aucun disque) pour etre digne de confiance.")


# ─────────── le cablage : « depose » ne veut pas dire « actif » ───────────

def test_floor_watch_cable_bien_la_sentinelle():
    from scraper import floor_watch as fw
    assert getattr(fw, "ss", None) is ss
    assert isinstance(getattr(fw, "SENTINELLE", None), ss.Sentinelle)
    src = open(fw.__file__, encoding="utf-8").read()
    corps = src.split("def _get(", 1)[1].split("\ndef ", 1)[0]
    assert "SENTINELLE.noter" in corps, (
        "_get n'alimente plus la sentinelle : elle compterait 0 requete et "
        "afficherait un 🟢 rassurant sur rien du tout.")
    assert "pause_conseillee" in corps, (
        "le backoff n'ecoute plus la sentinelle : elle mesure sans agir.")


def test_une_tentative_ne_compte_qu_une_fois(monkeypatch):
    """Le 429 etait compte deux fois (statut + exception) : le taux de refus
    doublait et la sentinelle criait sur son propre double comptage."""
    from scraper import floor_watch as fw

    class _Rep:
        status_code = 429
        def raise_for_status(self):
            raise RuntimeError("429 Client Error")
        def json(self):
            return {}

    class _Sess:
        def get(self, *a, **k):
            return _Rep()

    monkeypatch.setattr(fw, "SENTINELLE", ss.Sentinelle())
    monkeypatch.setattr(fw, "RETRIES", 1)
    fw._get("proc", None, _Sess())
    d = fw.SENTINELLE.obs["stackr"]
    assert d["total"] == 1, f"1 tentative comptee {d['total']} fois"
    assert d["repousse"] == 1
