# ⚠️ DEPOT : VeVePreda/scrapeur-veve   ·   CHEMIN : tests/test_floors_pont.py
"""Une seule source par champ dans 🟠H-PRIX.

CE QU'ON PROTEGE. `floors.py` (quotidien) et le pont veille→🟠H-PRIX (horaire,
via jetonveve) ecrivaient TOUS LES DEUX `market_lowestOffer`, chacun depuis son
fournisseur. Or `sheets.sync_dynamic` dedoublonne par uuid en comparant a la
DERNIERE VALEUR CONNUE — sans colonne de provenance. Deux fournisseurs qui
divergent produisent donc un va-et-vient A→B→A→B ou chaque alternance compte
comme un « changement » et s'append dans une serie temporelle.

MESURE DU 29/07 (🟠H-PRIX, 115 303 lignes) : 0,11 % de va-et-vient avant le
pont, 0,66 % apres — x6. Et ceder ne coute AUCUNE couverture : le pont voit
72,6 % des items encore en vente et 73,8 % des sold out.

⭐ POURQUOI UN TEST ET PAS SEULEMENT UN COMMENTAIRE : le mode dangereux est
celui qui s'obtient en NE FAISANT RIEN (le defaut). Ce test rend le retour
silencieux a « self » impossible sans qu'on l'ait voulu.
"""

import pathlib
import re

RACINE = pathlib.Path(__file__).resolve().parents[1]
SRC = (RACINE / "scraper" / "floors.py").read_text(encoding="utf-8")


def test_le_defaut_cede_le_floor_au_pont():
    m = re.search(r'os\.environ\.get\(\s*"FLOORS_FLOOR_SOURCE"\s*,\s*"(\w+)"', SRC)
    assert m, "le reglage FLOORS_FLOOR_SOURCE a disparu de floors.py"
    assert m.group(1) == "bridge", (
        f"le defaut est repasse a « {m.group(1)} » : floors.py se remet a "
        f"ecrire market_lowestOffer EN MEME TEMPS que le pont, et le "
        f"va-et-vient dans 🟠H-PRIX repart. Si c'est voulu (pont tombe), "
        f"il faut poser la variable, pas changer le defaut.")


def test_le_repli_vers_self_reste_possible():
    """Reversibilite : « bridge » ne doit pas etre un aller simple."""
    assert re.search(r'FLOORS_FLOOR_SOURCE".*?\)\.lower\(\)\s*!=\s*"self"', SRC), (
        "on ne peut plus revenir a floors.py en posant FLOORS_FLOOR_SOURCE="
        "self — une bascule sans retour n'est pas une bascule.")


def test_les_listings_restent_ecrits_par_floors():
    """Ceder le FLOOR ne doit pas ceder les LISTINGS : le pont ne les porte
    pas. Les perdre viderait une colonne de 🟠H-PRIX en silence."""
    assert 'item["market_totalListings"] = listings' in SRC
    bloc = SRC.split("if floor is not None and not cede_floor", 1)[1][:400]
    assert "market_totalListings" in bloc, (
        "l'ecriture des listings est passee sous la garde `cede_floor` : "
        "en mode pont, 🟠H-PRIX n'aurait plus AUCUN compte d'offres.")


def test_le_floor_nest_ecrit_que_si_on_ne_cede_pas():
    assert 'if floor is not None and not cede_floor:' in SRC, (
        "la garde qui empeche la double ecriture du floor a disparu.")
