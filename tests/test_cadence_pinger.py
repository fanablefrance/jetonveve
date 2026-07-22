# ⚠️ DEPOT : fanablefrance/jetonveve   ·   CHEMIN : tests/test_cadence_pinger.py
# Le projet vit sur 6 depots et DEUX comptes GitHub. Un fichier
# depose au mauvais endroit ne provoque aucune erreur : il dort.

"""🏓 CADENCE PINGER (22/07/2026) — les deux amenagements qui rendent une
cadence de 30 min possible SANS doubler l'empreinte ni perdre d'evenements.

1. floors_depuis_etat : un run qui demarre alors que le dernier balayage est
   frais (< FLOOR_REFRESH_MIN) REPREND les floors de `vfloors` au lieu de
   re-balayer ~76 pages de catalogue. Balayage lourd : 1x/h, quel que soit le
   nombre de runs.
2. Fenetre large au 1er tour (FLOOR_LISTINGS_FIRST) : le trou entre deux runs
   est couvert par le premier fetch — un evenement de l'entre-deux est
   rattrape (et date sur la carte), plus jamais perdu.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper import floor_watch as fw          # noqa: E402


def _etat(prev_age_min, ts):
    prev = ts - prev_age_min * 60
    return {
        "last_refresh_ts": prev,
        "vfloors": {
            "u1": [12.5, prev + 2],       # ecrit par CE balayage
            "u2": [0.0, prev + 2],        # sans floor, mais vu par le balayage
            "u-vieux": [99.0, prev - 7200],  # item sorti du catalogue : exclu
        },
    }


def test_un_balayage_frais_est_repris_de_l_etat(monkeypatch):
    monkeypatch.setattr(fw, "REFRESH_MIN", 60.0)
    ts = time.time()
    repris = fw.floors_depuis_etat(_etat(20, ts), ts)
    assert repris is not None
    veve, prev = repris
    assert veve["u1"] == 12.5
    assert "u2" in veve                    # 0.0 = vu sans floor, comme le vrai balayage
    assert "u-vieux" not in veve           # une vieille entree n'est pas un floor courant
    assert abs((ts - prev) / 60 - 20) < 1


def test_un_balayage_perime_force_le_re_balayage(monkeypatch):
    """> FLOOR_REFRESH_MIN : on rend None, le run re-balaye comme avant.
    C'est exactement le cas du cron horaire actuel — comportement inchange."""
    monkeypatch.setattr(fw, "REFRESH_MIN", 60.0)
    ts = time.time()
    assert fw.floors_depuis_etat(_etat(75, ts), ts) is None


def test_un_etat_vierge_force_le_re_balayage(monkeypatch):
    monkeypatch.setattr(fw, "REFRESH_MIN", 60.0)
    ts = time.time()
    assert fw.floors_depuis_etat({}, ts) is None
    assert fw.floors_depuis_etat({"last_refresh_ts": ts - 600}, ts) is None


def test_le_premier_tour_demande_la_fenetre_large(monkeypatch):
    """fetch_listings transmet bien la limite demandee a l'API (payload)."""
    vus = []

    def faux_get(proc, payload, session=None, meta=None):
        vus.append(payload)
        return {"items": []}

    monkeypatch.setattr(fw, "_get", faux_get)
    fw.fetch_listings(None, 120)
    fw.fetch_listings(None, fw.N_LISTINGS)
    assert vus[0]["limit"] == "120"
    assert vus[1]["limit"] == str(fw.N_LISTINGS)


def test_les_reglages_pinger_existent_et_ont_les_bons_defauts():
    """Defauts surs : fenetre 120 (une requete a peine plus grosse), reprise
    d'etat ACTIVE (no-op strict sur le cron horaire : l'etat y est toujours
    perime au demarrage)."""
    assert fw.N_LISTINGS_FIRST >= fw.N_LISTINGS
    assert fw.REFRESH_FROM_STATE is True
