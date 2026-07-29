# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_liquidite_cablee.py
"""Le filet qui aurait attrape la panne du 22→29/07.

CE QUI S'EST PASSE. `scraper/liquidity_baseline.py` a ete ecrit, teste et
DEPOSE le 22/07. Il n'a jamais ete importe par `floor_watch.py` : un rebase de
ce fichier a emporte le cablage. Pendant une semaine le module etait present,
correct, et INERTE — les tests passaient, les logs etaient propres, l'etat
avait l'air normal. Coût : ~2 880 requetes/jour vers StackR qui devaient
disparaitre.

⭐ LA LECON, PLUS LARGE QUE CE BUG : « depose » ne veut pas dire « actif ».
Un fichier present ne prouve RIEN. Le dernier test de ce fichier verifie donc
la famille entiere (`*_baseline.py`), pas seulement la liquidite : si un jour
on ajoute une 3e baseline et qu'on oublie de la brancher, ce test le dira.
"""

import pathlib
import re

import pytest

from scraper import floor_watch as fw
from scraper import liquidity_baseline as lb

RACINE = pathlib.Path(__file__).resolve().parents[1]


# ───────────────────────── le cablage lui-meme ─────────────────────────

def test_floor_watch_importe_bien_la_baseline_de_liquidite():
    """LA regression. `lb` doit etre un attribut du module, pas un souvenir."""
    assert getattr(fw, "lb", None) is lb, (
        "floor_watch n'importe plus liquidity_baseline. C'est EXACTEMENT la "
        "panne du 22/07 : le module reste depose, les tests passent, et la "
        "preuve de vente repart chercher 120 pages chez StackR a chaque run.")


def test_la_baseline_sert_de_preuve_quand_il_n_y_a_pas_de_vente_live(monkeypatch):
    monkeypatch.setattr(fw, "LIQUIDITE", {"u1": {"n_sales_90d": 4}})
    monkeypatch.setattr(fw, "PREUVE", {"live": 0, "entrepot": 0})
    assert fw._a_vente("u1", None) is True
    assert fw.PREUVE["entrepot"] == 1, "la provenance doit etre comptee"


def test_sans_vente_live_ni_entrepot_on_se_tait(monkeypatch):
    monkeypatch.setattr(fw, "LIQUIDITE", {"u1": {"n_sales_90d": 0}})
    monkeypatch.setattr(fw, "PREUVE", {"live": 0, "entrepot": 0})
    assert fw._a_vente("u1", None) is False
    assert fw._a_vente("inconnu", None) is False


def test_la_vente_live_prime_et_reste_comptee_a_part(monkeypatch):
    monkeypatch.setattr(fw, "LIQUIDITE", {})
    monkeypatch.setattr(fw, "PREUVE", {"live": 0, "entrepot": 0})
    assert fw._a_vente("u1", [12.5, 0, "2026-07-28"]) is True
    assert fw.PREUVE == {"live": 1, "entrepot": 0}


# ───────────────── la degradation gracieuse (le vrai garde-fou) ─────────────

def test_baseline_absente_on_repagine_comme_avant(monkeypatch):
    """Si la release est injoignable, on NE DOIT PAS rester a 5 pages : on
    perdrait la preuve ET on se tairait, sans que rien ne le dise."""
    monkeypatch.setattr(fw, "LIQUIDITE", {})
    assert fw.pages_de_ventes() == fw.SALES_PAGES


def test_baseline_presente_la_pagination_tombe(monkeypatch):
    monkeypatch.setattr(fw, "LIQUIDITE", {"u1": {"n_sales_90d": 1}})
    assert fw.pages_de_ventes() == fw.SALES_PAGES_LIQ
    assert fw.SALES_PAGES_LIQ < fw.SALES_PAGES


def test_load_liquidity_ne_leve_jamais(tmp_path):
    """Une baseline manquante est un cas NORMAL, pas une exception."""
    assert lb.load_liquidity(str(tmp_path / "nexiste_pas.csv.gz")) == {}


# ─────────────────── preuve ≠ prix (le piege a ne pas retomber) ─────────────

def test_la_baseline_ne_porte_aucun_prix():
    """La chaine ne grave pas les prix. Si un jour une colonne de prix
    apparait ici, quelqu'un aura invente un chiffre."""
    interdits = {"prix", "price", "usd", "omi", "floor", "value"}
    champs = set(lb._INT_COLS) | {"last_sale_date", "sales_per_day_90d"}
    for c in champs:
        assert not (interdits & set(c.lower().split("_"))), (
            f"colonne « {c} » : la baseline de liquidite est une PREUVE, "
            f"jamais un PRIX.")


def test_les_trois_verrous_require_sale_passent_par_a_vente():
    """Les 3 endroits qui exigent une preuve doivent tous appeler `_a_vente`.
    Si l'un revient a `last is None`, il redevient aveugle a l'entrepot."""
    src = (RACINE / "scraper" / "floor_watch.py").read_text(encoding="utf-8")
    corps = src.split("def _a_vente", 1)[1].split("\ndef ", 1)[1]
    verrous = re.findall(r"REQUIRE_SALE and (?:not )?(\w+)", corps)
    assert verrous, "aucun verrou REQUIRE_SALE trouve — le fichier a change"
    for v in verrous:
        assert v == "_a_vente", (
            f"un verrou REQUIRE_SALE teste « {v} » au lieu de _a_vente : il "
            f"ignore la preuve d'entrepot et se taira a tort.")


# ───────────── le filet generique : aucune baseline ne dort ─────────────

def test_aucune_baseline_deposee_ne_dort_sans_consommateur():
    """« Depose » ≠ « actif ». Toute `scraper/*_baseline.py` doit etre importee
    par au moins un module de production (hors tests, hors elle-meme)."""
    scraper = RACINE / "scraper"
    baselines = sorted(p.stem for p in scraper.glob("*_baseline.py"))
    assert baselines, "plus aucune baseline : le test a perdu son objet"
    modules = [p for p in scraper.glob("*.py") if not p.name.startswith("test_")]
    orphelines = []
    for b in baselines:
        motif = re.compile(rf"\b(?:import\s+{b}\b|from\s+scraper\s+import\s+"
                           rf"[^\n]*\b{b}\b)")
        if not any(motif.search(m.read_text(encoding="utf-8"))
                   for m in modules if m.stem != b):
            orphelines.append(b)
    assert not orphelines, (
        "Ces baselines sont DEPOSEES mais importees par PERSONNE — elles ne "
        "servent a rien et rien ne le signale :\n"
        + "\n".join(f"  · scraper/{o}.py" for o in orphelines)
        + "\n(c'est la panne du 22/07, a l'identique)")


def test_main_ecrit_bien_dans_la_globale_pas_dans_une_locale():
    """⚠️ LE PIÈGE LE PLUS SILENCIEUX DE CE LOT. `LIQUIDITE = lb.load_...()`
    dans main() SANS le `global` cree une variable LOCALE : la globale reste
    vide, `pages_de_ventes()` renvoie toujours 120 et `_a_vente` n'interroge
    jamais l'entrepot. Aucune erreur, aucun log different — le recablage
    serait defait par une seule ligne manquante, exactement comme il l'a ete
    une premiere fois."""
    import ast

    src = (RACINE / "scraper" / "floor_watch.py").read_text(encoding="utf-8")
    arbre = ast.parse(src)
    main = next((n for n in arbre.body
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main is not None, "main() a disparu de floor_watch"

    globales = {nom for n in ast.walk(main)
                if isinstance(n, ast.Global) for nom in n.names}
    assigne = any(isinstance(n, ast.Assign)
                  and any(isinstance(c, ast.Name) and c.id == "LIQUIDITE"
                          for t in n.targets for c in ast.walk(t))
                  for n in ast.walk(main))
    assert assigne, "main() ne charge plus la baseline de liquidite"
    assert "LIQUIDITE" in globales, (
        "main() assigne LIQUIDITE sans `global LIQUIDITE` : la globale "
        "restera vide et l'entrepot ne servira JAMAIS, en silence.")
