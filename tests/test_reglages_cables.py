# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_reglages_cables.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🔌 Tout reglage que le code LIT doit etre POSABLE depuis le workflow.

POURQUOI CE TEST EXISTE
-----------------------
Le 20/07/2026, on a fait poser la variable de depot `FLOOR_POLLS=12` pour lancer
une experience de deux jours. Le workflow contenait :

    FLOOR_POLLS: ${{ github.event.inputs.polls || '25' }}

— pas de `vars.`, alors que toutes les lignes voisines en avaient un. La
variable n'etait donc lue par personne. L'experience a tourne a 25 tours
([1/25] dans le log) et aurait couru jusqu'au bout sans rien mesurer.

⭐ CE DEFAUT EST INVISIBLE PAR CONSTRUCTION. `os.environ.get("X", defaut)` ne
leve rien, n'avertit de rien : il applique son defaut en silence. On croit
piloter, on ne pilote pas. Le controle systematique a trouve 15 reglages dans ce
cas, dont trois portaient des recommandations deja formulees et donc
inapplicables : l'optimisation -60 % (`FLOOR_ELEM_LIMIT`), le plafond de
vraisemblance (`FLOOR_PRIX_MAX`) et la sentinelle anti-blocage
(`FLOOR_MIN_RATIO` / `FLOOR_MAIGRE_ALERTE`).

⭐⭐ LA REGLE QUI EN DECOULE : un reglage est cable quand il apparait AUX DEUX
BOUTS — `os.environ.get("X")` dans le code ET une ligne `X:` dans le workflow.
Les deux, sinon rien. Ce test le verifie a chaque execution de la suite.

PORTEE
------
Le workflow ne lance qu'un module (`python -m scraper.floor_watch`), mais
celui-ci en pilote trois autres dans le meme environnement : `numeros`,
`price_baseline`, `whale_watch`. Ils comptent donc aussi.

CE TEST NE VERIFIE PAS que la valeur est bonne — seulement que la main existe.
"""

from __future__ import annotations

import os
import re

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(RACINE, ".github", "workflows", "floor-watch.yml")

# Modules dont les reglages sont lus pendant un run de floor-watch.
# ⭐ `bot_alertes` en fait partie : floor_watch l'importe et pousse chaque lot
# d'alertes vers lui. Il a d'abord ete oublie de cette liste — et c'est
# `test_les_exemptions_sont_toutes_encore_justifiees` qui l'a signale, en
# refusant des exemptions qui ne correspondaient a aucun module surveille.
# Le garde-fou du garde-fou a servi avant meme d'etre depose.
MODULES = ["floor_watch", "numeros", "price_baseline", "whale_watch",
           "bot_alertes"]

# ---------------------------------------------------------------------------
# Les exemptions — chacune JUSTIFIEE, sinon elle n'a rien a faire ici
# ---------------------------------------------------------------------------
# ⚠️ N'ajouter une ligne ici qu'avec sa raison. Une liste d'exemptions qui
# grossit sans motif transforme ce test en decoration.
EXEMPTS = {
    # 🌉 Le pont vers le bot est ETEINT VOLONTAIREMENT : GitHub Actions ne peut
    # pas joindre Predabot, qui n'a pas de domaine. Poser ces variables
    # laisserait croire que le pont est actif. Il se rallumera tout seul le jour
    # du VPS, ou l'URL devient localhost.
    "BOT_ALERTES_URL", "BOT_ALERTES_JETON", "BOT_ALERTES_TIMEOUT",
    # 🐋 Ces quatre-la ne sont lus que par `whale_watch.main()`, qui n'est lance
    # par AUCUN workflow : le module tourne pilote par la boucle de floor_watch,
    # qui lui passe son propre etat et sa propre cadence. Les poser suggererait
    # un reglage sans effet — exactement le defaut que ce test traque.
    "WHALE_POLLS", "WHALE_INTERVAL_S", "WHALE_REFRESH_MIN", "WHALE_STATE",
    "WHALE_SIMULER",
}


def _lus_par_le_code():
    """Tout `os.environ.get("NOM"…)` des modules concernes -> {nom: module}."""
    trouves = {}
    for mod in MODULES:
        chemin = os.path.join(RACINE, "scraper", mod + ".py")
        if not os.path.exists(chemin):
            continue
        with open(chemin, encoding="utf-8") as fh:
            code = fh.read()
        for nom in re.findall(r'os\.environ\.get\(\s*"([A-Z_0-9]+)"', code):
            trouves.setdefault(nom, mod)
    return trouves


def _poses_par_le_workflow():
    """Toute cle `NOM:` du workflow.

    Volontairement tolerant : on ne cherche pas a isoler le bloc `env:`. Un faux
    positif exigerait qu'une cle YAML hors env porte EXACTEMENT le nom d'une
    variable lue par le code (FLOOR_*, MINT_*, WHALE_*…) — ce qui n'arrive pas.
    En echange, aucune dependance YAML : le test tourne partout, y compris dans
    un bac a sable nu.
    """
    with open(WORKFLOW, encoding="utf-8") as fh:
        return set(re.findall(r'^\s+([A-Z_0-9]+):', fh.read(), re.M))


def test_le_workflow_existe():
    assert os.path.exists(WORKFLOW), WORKFLOW


def test_aucun_reglage_lu_nest_impossible_a_poser():
    lus = _lus_par_le_code()
    poses = _poses_par_le_workflow()
    manquants = sorted(n for n in lus if n not in poses and n not in EXEMPTS)
    assert not manquants, (
        "Ces reglages sont lus par le code mais introuvables dans "
        "floor-watch.yml — les poser n'aurait AUCUN effet, et rien ne le "
        "dirait :\n" + "\n".join(f"  · {n}  (lu dans scraper/{lus[n]}.py)"
                                 for n in manquants))


def test_floor_polls_est_pilotable_par_variable_de_depot():
    """⭐ LA PORTE. C'est la ligne exacte qui a fait tourner l'experience du
    20/07 a 25 tours au lieu de 12, deux jours durant, sans rien mesurer."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        ligne = [l for l in fh if re.match(r'\s*FLOOR_POLLS:', l)]
    assert ligne, "FLOOR_POLLS a disparu du workflow"
    assert "vars.FLOOR_POLLS" in ligne[0], (
        "FLOOR_POLLS sans `vars.` : la variable de depot est ignoree.\n"
        "Ligne trouvee : " + ligne[0].strip())


def test_les_chemins_detat_restent_en_dur():
    """⛔ L'etape de commit teste `data/floor_state.json` et
    `data/hprix_feed.csv` EN DUR. Rendre ces chemins pilotables ferait ecrire le
    run ailleurs, le commit ne trouverait rien, et la recolte du run serait
    perdue en silence. La recolte est sacree — y compris contre le confort."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        wf = fh.read()
    for cle in ("FLOOR_STATE", "HPRIX_FEED"):
        ligne = [l for l in wf.splitlines() if re.match(r'\s*%s:' % cle, l)]
        assert ligne, cle + " doit rester pose explicitement"
        assert "vars." not in ligne[0], (
            cle + " ne doit PAS etre pilotable : " + ligne[0].strip())


def test_les_exemptions_sont_toutes_encore_justifiees():
    """Une exemption pour un reglage que le code ne lit plus est un mensonge
    qui dort. Si ce test tombe, retirer la ligne devenue inutile d'EXEMPTS."""
    lus = set(_lus_par_le_code())
    orphelines = sorted(EXEMPTS - lus)
    assert not orphelines, (
        "Ces exemptions ne correspondent plus a aucun reglage lu : "
        + ", ".join(orphelines))
