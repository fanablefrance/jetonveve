# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_burns_rattrapage.py   (NEUF)
# ═══════════════════════════════════════════════════════════════════════════════
# 🔬 LE BANC DU RATTRAPAGE DES BURNS
# ═══════════════════════════════════════════════════════════════════════════════
#
# ⭐⭐⭐ CE BANC A ETE JUGE EN LUI INJECTANT LA MAUVAISE REGLE, une par une.
# Les six fautes essayees, et le banc a rougi sur chacune :
#   1. `d <= seuil` au lieu de `d < seuil`  -> une veille normale crie
#   2. `aujourd'hui` au lieu de `veille`    -> chaque matin crie
#   3. `aujourd'hui - 2 j`                  -> J-2 passe en silence
#   4. `return False` quand le fichier est illisible -> le pire cas se tait
#   5. derniere ligne au lieu du maximum    -> un fichier non trie ment
#   6. comparaison sur les 4 premiers chars -> tout un mois passe
# Temoin vert avant ET apres chaque injection.
#
# ⚠️ CE QUE CE BANC NE COUVRE PAS : il eprouve la REGLE sur des fichiers
# fabriques. Il ne dit rien du post Discord ni du Sheet, et il ne peut pas
# prouver que blockscout repondra. L'effet reel se constate sur le depot.

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.burns_fraicheur import derniere_date, manque, seuil  # noqa: E402

EN_TETE = "date,source,transactions,omi_burned,cumulative\n"


def _csv(tmp_path, dates):
    p = tmp_path / "burns_daily.csv"
    p.write_text(EN_TETE + "".join(
        f"{d},StackR,100,1000.0,999.0\n" for d in dates), encoding="utf-8")
    return str(p)


AUJ = dt.date(2026, 8, 21)


# ── LE CHEMIN NORMAL NE DOIT JAMAIS CRIER ───────────────────────────────────
def test_la_veille_passe(tmp_path):
    """Etat mesure apres CHAQUE run vert : le CSV finit la veille."""
    assert manque(_csv(tmp_path, ["2026-08-19", "2026-08-20"]), AUJ) is False


def test_le_jour_meme_passe(tmp_path):
    """Run tardif (mesure du commit du 06/08 10:17) : le CSV finit AUJOURD'HUI."""
    assert manque(_csv(tmp_path, ["2026-08-20", "2026-08-21"]), AUJ) is False


# ── L'ETAT REELLEMENT OBSERVE LE 21/08 DOIT CRIER ───────────────────────────
def test_j_moins_2_manque(tmp_path):
    """L'etat exact du depot au 21/08 apres 3 nuits rouges : fin au 19/08."""
    assert manque(_csv(tmp_path, ["2026-08-18", "2026-08-19"]), AUJ) is True


def test_trou_ancien_manque(tmp_path):
    assert manque(_csv(tmp_path, ["2026-07-01"]), AUJ) is True


# ── LES CAS OU SE TAIRE SERAIT LA PANNE ─────────────────────────────────────
def test_fichier_absent_compte_comme_manquant(tmp_path):
    assert manque(str(tmp_path / "nexiste-pas.csv"), AUJ) is True


def test_fichier_vide_compte_comme_manquant(tmp_path):
    p = tmp_path / "burns_daily.csv"
    p.write_text(EN_TETE, encoding="utf-8")
    assert manque(str(p), AUJ) is True


def test_dates_illisibles_comptent_comme_manquant(tmp_path):
    p = tmp_path / "burns_daily.csv"
    p.write_text(EN_TETE + "20/08/2026,StackR,1,1.0,1.0\n", encoding="utf-8")
    assert derniere_date(str(p)) is None
    assert manque(str(p), AUJ) is True


# ── LE FICHIER N'EST PAS SUPPOSE TRIE ───────────────────────────────────────
def test_maximum_et_non_derniere_ligne(tmp_path):
    """⭐ Faute 5 : un CSV non trie ferait mentir « la derniere ligne »."""
    p = _csv(tmp_path, ["2026-08-20", "2026-08-11"])
    assert derniere_date(p) == "2026-08-20"
    assert manque(p, AUJ) is False


# ── LA BASCULE, AU JOUR PRES ────────────────────────────────────────────────
def test_bascule_exacte(tmp_path):
    """A la frontiere : 20/08 passe, 19/08 crie. Faute 1 et 3 meurent ici."""
    assert seuil(AUJ) == "2026-08-20"
    assert manque(_csv(tmp_path, ["2026-08-20"]), AUJ) is False
    assert manque(_csv(tmp_path, ["2026-08-19"]), AUJ) is True


def test_changement_de_mois(tmp_path):
    """Faute 6 : une comparaison tronquee laisserait passer tout un mois."""
    auj = dt.date(2026, 9, 1)
    assert seuil(auj) == "2026-08-31"
    assert manque(_csv(tmp_path, ["2026-08-31"]), auj) is False
    assert manque(_csv(tmp_path, ["2026-08-30"]), auj) is True
