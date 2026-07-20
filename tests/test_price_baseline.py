"""Tests price_baseline — chargement, percentiles, detecteurs (OFF/ON, cooldown,
preuve de vente, garde-fous). Aucun reseau."""
import csv
import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
import price_baseline as pb  # noqa: E402

UID = "aaaaaaaa-1111-2222-3333-444444444444"

BL = {UID: {"n_points": 200, "floor_min": 100.0, "floor_p5": 150.0,
            "floor_p25": 300.0, "floor_p50": 500.0, "floor_p75": 700.0,
            "floor_p95": 900.0, "floor_max": 1000.0, "listings_p50": 4.0,
            "listings_p90": 8.0, "listings_max": 12.0, "last_floor": 500.0,
            "last_listings": 4.0}}


def _cat():
    return {UID: {"name": "Spidey", "categorie": "collectible"}}


# --- chargement ------------------------------------------------------------

def test_load_local_gz(tmp_path):
    p = str(tmp_path / "b.csv.gz")
    cols = ["veve_uuid", "n_points", "floor_min", "floor_p5", "floor_p25",
            "floor_p50", "floor_p75", "floor_p95", "floor_max", "listings_p50",
            "listings_p90", "listings_max", "last_floor", "last_listings"]
    with gzip.open(p, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow([UID, 200, 100, 150, 300, 500, 700, 900, 1000, 4, 8, 12, 500, 4])
    got = pb.load_baselines(p)
    assert UID in got and got[UID]["floor_p50"] == 500.0 and got[UID]["n_points"] == 200


def test_load_absent_ne_crashe_pas(tmp_path):
    assert pb.load_baselines(str(tmp_path / "nope.csv.gz")) == {}


# --- percentiles -----------------------------------------------------------

def test_pct_rank():
    assert pb.pct_rank(BL[UID], 100) == 0.0
    assert pb.pct_rank(BL[UID], 1000) == 100.0
    assert pb.pct_rank(BL[UID], 500) == 50.0
    r = pb.pct_rank(BL[UID], 400)          # entre p25(300) et p50(500)
    assert 25 < r < 50


def test_is_hist_low_high():
    assert pb.is_hist_low(BL[UID], 140, pct=10)      # sous p5
    assert not pb.is_hist_low(BL[UID], 500, pct=10)
    assert pb.is_hist_high(BL[UID], 950, pct=90)


def test_vol_ratio():
    assert pb.vol_ratio(BL[UID], 8) == 2.0
    assert pb.vol_ratio(BL[UID], None) is None


# --- detect_hist_low -------------------------------------------------------

def test_hist_low_off_ne_declenche_pas():
    st = {}
    out = pb.detect_hist_low(st, {UID: 120}, BL, _cat(), {UID: [50]}, ts=1.7e9,
                             on=False)
    assert out == []


# ⚠️ 20/07/2026 — 📊 et 🔊 ne signalent plus un ETAT mais une ENTREE dans la
# bande. Consequence sur ces tests : il faut d'abord un passage HORS bande
# (l'amorcage), sinon le detecteur n'a rien a quoi comparer et se tait.
# Ce n'est pas une contrainte de test, c'est le correctif lui-meme : sans
# amorcage, le premier run publiait tout le stock d'un coup et le garde-fou
# anti-avalanche bloquait 📊 definitivement. Voir tests/test_histlow_transition.py.

def _amorcer(st, prix=500):
    """Un passage hors bande, pour que l'entree suivante en soit une."""
    pb.detect_hist_low(st, {UID: prix}, BL, _cat(), {UID: [50]}, ts=1.7e9 - 7200,
                       on=True, pct=10)


def test_hist_low_declenche_avec_preuve():
    st = {}
    _amorcer(st)
    out = pb.detect_hist_low(st, {UID: 120}, BL, _cat(), {UID: [50]}, ts=1.7e9,
                             on=True, pct=10)
    assert len(out) == 1 and out[0]["uuid"] == UID and out[0]["rank"] <= 5


def test_hist_low_sans_vente_ecarte():
    st = {}
    _amorcer(st)
    out = pb.detect_hist_low(st, {UID: 120}, BL, _cat(), sales={}, ts=1.7e9,
                             on=True, require_sale=True)
    assert out == []


def test_hist_low_pas_assez_historique():
    bl = {UID: dict(BL[UID], n_points=5)}
    st = {}
    pb.detect_hist_low(st, {UID: 500}, bl, _cat(), {UID: [50]}, ts=1.7e9 - 7200,
                       on=True, min_points=30)
    out = pb.detect_hist_low(st, {UID: 120}, bl, _cat(), {UID: [50]}, ts=1.7e9,
                             on=True, min_points=30)
    assert out == []


def test_hist_low_cooldown():
    st = {}
    _amorcer(st)
    a = pb.detect_hist_low(st, {UID: 120}, BL, _cat(), {UID: [50]}, ts=1.7e9,
                           on=True)
    b = pb.detect_hist_low(st, {UID: 120}, BL, _cat(), {UID: [50]},
                           ts=1.7e9 + 60, on=True)          # <6h -> mute
    assert len(a) == 1 and b == []


# --- detect_vol_anomaly ----------------------------------------------------

def test_vol_anomaly_declenche_au_dessus_norme():
    st = {}
    pb.detect_vol_anomaly(st, {UID: 2}, BL, _cat(), ts=1.7e9 - 7200,
                          on=True, ratio=3.0)               # amorcage, sous la norme
    out = pb.detect_vol_anomaly(st, {UID: 15}, BL, _cat(), ts=1.7e9,
                                on=True, ratio=3.0)         # 15 >= 4*3 et >= p90(8)
    assert len(out) == 1 and out[0]["ratio"] == 3.8


def test_vol_anomaly_norme_ne_declenche_pas():
    out = pb.detect_vol_anomaly({}, {UID: 5}, BL, _cat(), ts=1.7e9, on=True,
                                ratio=3.0)
    assert out == []
