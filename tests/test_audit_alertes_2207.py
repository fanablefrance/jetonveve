# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_audit_alertes_2207.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🔬 AUDIT DU 22/07/2026 — chaque test REPRODUIT un defaut vu en production.

Les quatre plaintes de Preda, et le defaut mesure derriere chacune :

1. « Plus-bas historique : 369 $ » sous un floor a 8,64 $ (Jason Naylor).
   -> elements.csv porte des decimales FR ×100 gelees (16,9 % des paires
      atl/ath sont IMPOSSIBLES : atl > ath). Et les cartes affichaient l'ATL
      CATALOGUE BRUT sans le recouper avec le plus-bas OBSERVE (`atl_seen`).
2. Alerte 📉 pour Borg Cube a 4,75 $ contre un ancien ATL de 4,80 $ (−1 %).
   -> ATL_MARGIN_PCT etait definie, cablee dans le workflow, documentee…
      et JAMAIS LUE par detect_atl.
3. « Offres en vente : 0 — offre unique » : un compte de la VEILLE affiche
   comme s'il incluait le listing tout frais.
4. Evenements 🐋 publies 2 h 30 apres les faits (mesure : listes 10:00-10:08
   UTC, publies 12:34-12:36) SANS heure de l'evenement sur la carte — et
   cartes 🐋 sans floor ni plus-bas.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import floor_watch as fw          # noqa: E402
from scraper import whale_watch as ww          # noqa: E402


# ---------------------------------------------------------------------------
# 1a. La garde de coherence du catalogue : atl > ath = paire INCONNUE
# ---------------------------------------------------------------------------

def _csv_elements(tmp_path, lignes):
    entete = ("veve_uuid,series_uuid,name,category,rarity,edition_type,"
              "supply,first_public,listings,note,brand,licensor,"
              "atl,atl_date,ath,ath_date\n")
    p = tmp_path / "elements.csv"
    p.write_text(entete + "\n".join(lignes) + "\n", encoding="utf-8")
    return str(p)


def test_une_paire_atl_ath_impossible_est_jugee_inconnue(tmp_path):
    """Jason Naylor, tel qu'exporte en prod : atl=369, ath=20. IMPOSSIBLE
    (un plus-bas au-dessus du plus-haut) -> les DEUX passent a None."""
    chemin = _csv_elements(tmp_path, [
        "u-jason,s1,Jason Naylor - OPN Heart COLOR,collectible,UR,CE,"
        "555,21,4,,DesignerCon,DC,369,2026-07-12,20,2024-11-16",
    ])
    cat = fw.charger_elements(chemin)
    assert cat["u-jason"]["atl"] is None
    assert cat["u-jason"]["ath"] is None


def test_une_paire_coherente_et_les_virgules_fr_passent(tmp_path):
    chemin = _csv_elements(tmp_path, [
        # « 6,99 » : la virgule FR doit donner 6.99, jamais 699.
        'u-spidey,s2,Spider-Man 548,comic,COMMON,548,1000,100,0,,ASM,Marvel,'
        '"6,99",2026-07-21,"9,50",2026-07-21',
    ])
    cat = fw.charger_elements(chemin)
    assert cat["u-spidey"]["atl"] == 6.99
    assert cat["u-spidey"]["ath"] == 9.5


def test_un_atl_seul_sans_ath_est_garde(tmp_path):
    """La garde ne juge que les paires COMPLETES : un atl seul reste (on ne
    peut pas prouver qu'il est corrompu sans son ath)."""
    chemin = _csv_elements(tmp_path, [
        "u-solo,s3,Item,collectible,C,FA,100,1,2,,M,L,4.5,2026-07-01,,",
    ])
    assert fw.charger_elements(chemin)["u-solo"]["atl"] == 4.5


# ---------------------------------------------------------------------------
# 1b. atl_connu : ce qui S'AFFICHE = ce que le detecteur CROIT
# ---------------------------------------------------------------------------

def test_atl_connu_recoupe_catalogue_et_observation():
    """Le cas de prod : catalogue corrompu a 369, module ayant OBSERVE 8,62.
    La carte doit dire 8,62 — jamais 369."""
    state = {"atl_seen": {"u1": [8.62, 1784529011.0]}}
    assert fw.atl_connu(state, "u1", 369) == 8.62


def test_atl_connu_prend_le_catalogue_quand_il_est_le_plus_bas():
    state = {"atl_seen": {"u1": [12.0, 0]}}
    assert fw.atl_connu(state, "u1", 2.0) == 2.0


def test_atl_connu_sans_aucune_reference_rend_none():
    assert fw.atl_connu({}, "u1", None) is None


def test_atl_connu_ignore_les_valeurs_invraisemblables(monkeypatch):
    """Au-dela du plafond de vraisemblance (50 000 $), une reference n'est
    pas une reference — meme regle que partout ailleurs."""
    monkeypatch.setattr(fw, "PRIX_MAX", 50000.0)
    state = {"atl_seen": {}}
    assert fw.atl_connu(state, "u1", 424204204204.0) is None


def test_detect_veve_steal_affiche_le_plus_bas_recoupe(monkeypatch):
    """Reproduction bout-en-bout de la carte Jason Naylor : floor qui chute
    de 12,79 a 8,64, catalogue corrompu (369), plus-bas observe 8,62.
    La carte 🩸 doit porter 8,62 — c'etait 369 en prod."""
    monkeypatch.setattr(fw, "STEAL_ON", True)
    monkeypatch.setattr(fw, "REQUIRE_SALE", False)
    ts = time.time()
    state = {
        "vfloors": {"u-jason": [12.79, ts - 3600]},
        "atl_seen": {"u-jason": [8.62, ts - 3600]},
    }
    cat = {"u-jason": {"name": "Jason Naylor - OPN Heart COLOR",
                       "categorie": "collectible", "atl": 369}}
    out = fw.detect_veve_steal(state, {"u-jason": 8.64}, cat, ts=ts)
    assert len(out) == 1
    assert out[0]["atl"] == 8.62
    carte = fw.carte_steal(out[0])
    assert "369" not in carte["description"]
    assert "8.62" in carte["description"]


# ---------------------------------------------------------------------------
# 2. detect_atl applique ENFIN sa marge de declenchement
# ---------------------------------------------------------------------------

def _etat_atl(ts, prev=4.80):
    return {"atl_seen": {"u-borg": [prev, ts - 3600]},
            "alerts_atl": {}, "sales": {}}


def test_atl_une_baisse_d_un_pour_cent_ne_tire_plus(monkeypatch):
    """Borg Cube, tel que publie en prod : 4,75 $ contre un ATL de 4,80 $
    (−1 %) avec ATL_MARGIN_PCT=25 pose dans le workflow. Ne doit PAS tirer."""
    monkeypatch.setattr(fw, "ATL_ON", True)
    monkeypatch.setattr(fw, "ATL_MARGIN_PCT", 25.0)
    ts = time.time()
    out = fw.detect_atl(_etat_atl(ts), {"u-borg": 4.75},
                        {"u-borg": {"name": "Borg Cube"}}, ts=ts)
    assert out == []


def test_atl_une_vraie_chute_sous_la_marge_tire(monkeypatch):
    monkeypatch.setattr(fw, "ATL_ON", True)
    monkeypatch.setattr(fw, "ATL_MARGIN_PCT", 25.0)
    ts = time.time()
    out = fw.detect_atl(_etat_atl(ts), {"u-borg": 3.50},
                        {"u-borg": {"name": "Borg Cube"}}, ts=ts)
    assert len(out) == 1 and out[0]["floor"] == 3.50


def test_atl_marge_zero_garde_l_ancien_comportement(monkeypatch):
    """ATL_MARGIN_PCT=0 : tout passage strictement sous l'ATL tire (le
    reglage reste pilotable, on ne fige rien)."""
    monkeypatch.setattr(fw, "ATL_ON", True)
    monkeypatch.setattr(fw, "ATL_MARGIN_PCT", 0.0)
    ts = time.time()
    out = fw.detect_atl(_etat_atl(ts), {"u-borg": 4.75},
                        {"u-borg": {"name": "Borg Cube"}}, ts=ts)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# 3. « Offres en vente : 0 — offre unique » ne peut plus s'ecrire
# ---------------------------------------------------------------------------

def _comic(n):
    return {"uuid": "u-c", "name": "Spider-Man 548", "usd": 1.69,
            "ou": "StackR", "supply": 1000, "listings": n,
            "rarity": "COMMON", "edition": "548", "serie": "s",
            "veve_floor": 1.79, "atl": None, "quand": None}


def test_zero_offre_connue_veut_dire_celle_ci_uniquement():
    desc = fw.carte_comic(_comic(0))["description"]
    assert "0 — offre unique" not in desc
    assert "celle-ci uniquement" in desc


def test_une_offre_connue_reste_une_offre_unique():
    desc = fw.carte_comic(_comic(1))["description"]
    assert "1" in desc and "offre unique" in desc


# ---------------------------------------------------------------------------
# 4a. L'age d'un evenement : parse, affichage, filtre
# ---------------------------------------------------------------------------

def test_event_epoch_lit_le_format_du_flux():
    e = fw._event_epoch({"timestamp": "2026-07-20T10:03:51.368Z"})
    assert e is not None
    # 10:03:51 UTC ce jour-la
    import datetime as dt
    d = dt.datetime.fromtimestamp(e, dt.timezone.utc)
    assert (d.hour, d.minute) == (10, 3)


def test_event_epoch_absent_ou_illisible_rend_none():
    assert fw._event_epoch({}) is None
    assert fw._event_epoch({"timestamp": "n/a"}) is None


def test_ligne_quand_dit_le_retard():
    ts = time.time()
    ligne = fw.ligne_quand("Listé", ts - 151 * 60, ts)   # 2 h 31
    # 22/07 : l'heure affichee est celle du LECTEUR (Europe/Paris), plus UTC —
    # Preda lisait « 11:56 UTC » sous un Discord a 13:56 et voyait 2 h de
    # retard imaginaire.
    assert "il y a 2 h 31" in ligne and "FR" in ligne and "UTC" not in ligne


def test_trop_vieux_ne_filtre_que_si_le_reglage_est_pose(monkeypatch):
    ts = time.time()
    vieux = ts - 150 * 60
    monkeypatch.setattr(fw, "EVENT_MAX_AGE_MIN", 0.0)      # defaut : off
    assert not fw.trop_vieux(vieux, ts)
    monkeypatch.setattr(fw, "EVENT_MAX_AGE_MIN", 45.0)
    assert fw.trop_vieux(vieux, ts)
    # un horodatage ABSENT ne filtre jamais : inconnu != vieux
    assert not fw.trop_vieux(None, ts)


def test_mints_filtre_les_listings_perimes(monkeypatch):
    """Le scenario ODDY : un listing vieux de 2 h ressurgit apres des runs
    sautes. Avec FLOOR_EVENT_MAX_AGE_MIN pose, il ne tire plus ; sans le
    reglage, il tire ET la carte porte son age."""
    ts = time.time()
    import datetime as dt
    vieux_iso = dt.datetime.fromtimestamp(
        ts - 150 * 60, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cat = {"u-oddy": {"name": "ODDY ver. VEVE", "categorie": "collectible",
                      "supply": 30, "premiere": 6, "rarity": "COMMON",
                      "note": "", "serie": "s", "atl": None,
                      "marque": "", "licence": ""}}
    listing = {"element_id": "u-oddy", "edition": 1, "price": 1000,
               "nft_id": "n1", "timestamp": vieux_iso,
               "stackr_floor_price": 1000}
    monkeypatch.setattr(fw, "EVENT_MAX_AGE_MIN", 45.0)
    assert fw.detect_mints({}, cat, [listing], 0.0016, ts=ts) == []
    monkeypatch.setattr(fw, "EVENT_MAX_AGE_MIN", 0.0)
    out = fw.detect_mints({}, cat, [listing], 0.0016, ts=ts)
    assert len(out) == 1
    assert "il y a 2 h" in fw.carte_mint(out[0])["description"]


# ---------------------------------------------------------------------------
# 4b. Les cartes 🐋 portent enfin le contexte : floor, plus-bas, age
# ---------------------------------------------------------------------------

def _evt_whale(ts, age_min=150):
    import datetime as dt
    stamp = dt.datetime.fromtimestamp(
        ts - age_min * 60, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    listing = {"nft_id": "1316", "element_id": "u-zg", "name": "Zero Ghost #1",
               "edition": 1316, "price": 11000, "timestamp": stamp,
               "element_type": "COMIC_COVER", "listed_by": "0xabc",
               "listed_by_username": "Josh7291"}
    tracked = ({"0xabc": {"username": "Josh7291", "type": "VeVe Team"}}, {})
    return listing, tracked


def test_la_carte_whale_porte_floor_atl_et_age():
    ts = time.time()
    listing, tracked = _evt_whale(ts)
    state = {"atl_seen": {"u-zg": [3.5, ts - 3600]}, "sales": {}}
    veve = {"u-zg": 2.10}
    cat = {"u-zg": {"name": "Zero Ghost #1", "atl": None}}
    out = ww.detect_marche(state, [listing], [], tracked, 0.000162,
                           veve=veve, cat=cat)
    assert len(out) == 1
    a = out[0]
    assert a["floor_veve"] == 2.10
    assert a["atl"] == 3.5
    desc = ww.carte(a)["description"]
    assert "Floor VeVe" in desc
    assert "Plus-bas historique" in desc
    assert "il y a 2 h" in desc


def test_whale_respecte_le_filtre_de_fraicheur(monkeypatch):
    ts = time.time()
    listing, tracked = _evt_whale(ts, age_min=150)
    monkeypatch.setattr(fw, "EVENT_MAX_AGE_MIN", 45.0)
    out = ww.detect_marche({}, [listing], [], tracked, 0.000162)
    assert out == []


def test_whale_sans_contexte_reste_compatible():
    """L'appel historique (sans veve/cat) doit continuer de marcher : le
    module standalone et les vieux tests l'utilisent."""
    ts = time.time()
    listing, tracked = _evt_whale(ts, age_min=1)
    out = ww.detect_marche({}, [listing], [], tracked, 0.000162)
    assert len(out) == 1
    assert out[0].get("floor_veve") is None
