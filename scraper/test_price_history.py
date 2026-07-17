"""Tests price_history — filtrage remplissage, on-change, fenetrage, reprise,
recolte sacree, append. Aucun reseau : HTTP_GET est remplace par un faux serveur.
"""
import csv
import datetime as dt
import gzip
import os
import sys
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
import price_history as ph  # noqa: E402

UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-5555-6666-7777-888888888888"
UUID_KO = "cccccccc-9999-0000-0000-000000000000"

# Historique reel simule (vraies obs). Le faux serveur y ajoute du remplissage.
HIST = {
    UUID_A: [
        ("2026-01-02T01:06:00.0", 8500, 4),
        ("2026-01-03T02:07:00.0", 8500, 4),   # inchange -> compresse
        ("2026-01-10T03:08:00.0", 6900, 6),   # change floor+listings
        ("2026-05-01T04:09:00.0", 6900, 7),   # change listings seul
    ],
    UUID_B: [
        ("2026-02-01T00:00:00.0", 100, 1),
        ("2026-02-02T00:00:00.0", 120, 1),
    ],
}


def _fake_http(url):
    """Sert NftPriceMetrics : vraies obs dans la fenetre + lignes de remplissage.
    UUID_KO simule une API definitivement en panne (None)."""
    q = parse_qs(urlparse(url).query)
    guid = q["guid"][0]
    if guid == UUID_KO:
        return None, "HTTP 500"
    frm = dt.datetime.strptime(q["fromTimeStamp"][0][:10], "%Y-%m-%d").date()
    to = dt.datetime.strptime(q["toTimeStamp"][0][:10], "%Y-%m-%d").date()
    rows = []
    for ts, floor, listings in HIST.get(guid, []):
        d = dt.datetime.strptime(ts[:10], "%Y-%m-%d").date()
        if frm <= d < to:
            rows.append({"nftUuid": guid, "createdTimestamp": ts,
                         "lowestMarketPrice": floor, "totalMarketListings": listings})
            # ligne de REMPLISSAGE le lendemain (doit etre ignoree)
            rows.append({"nftUuid": ph.FILLER_UUID,
                         "createdTimestamp": ts[:10] + "T12:00:00",
                         "lowestMarketPrice": floor, "totalMarketListings": listings})
    return rows, ""


def setup_function(_):
    ph.HTTP_GET = _fake_http
    ph.BACKOFF = 0            # pas d'attente en test
    ph.PAUSE = 0
    ph.WINDOW_DAYS = 120
    ph.RETRIES = 2
    ph.MIN_WINDOW_DAYS = 15
    ph.WORKERS = 2
    ph.FLUSH_ITEMS = 300
    ph.MAX_ITEMS = 0


# --- unitaires -------------------------------------------------------------

def test_fetch_window_filtre_le_remplissage():
    rows, err = ph.fetch_window(UUID_A, dt.date(2026, 1, 1), dt.date(2026, 1, 5))
    assert err == ""
    assert rows and all(r["nftUuid"] == UUID_A for r in rows)   # zero filler


def test_compress_on_change():
    obs = [{"createdTimestamp": t, "lowestMarketPrice": f, "totalMarketListings": l}
           for t, f, l in HIST[UUID_A]]
    out = ph.compress(UUID_A, obs)
    # 4 obs -> 3 points (le 2e, inchange, est fondu)
    assert [(o[2], o[3]) for o in out] == [(8500, 4), (6900, 6), (6900, 7)]
    assert all(o[0] == UUID_A for o in out)


def test_compress_seed_evite_le_doublon():
    obs = [{"createdTimestamp": "2026-02-01T00:00:00", "lowestMarketPrice": 100,
            "totalMarketListings": 1}]
    assert ph.compress(UUID_B, obs, seed=(100.0, 1)) == []      # identique au seed


# --- backfill bout-en-bout -------------------------------------------------

def _cat_rows():
    return [{"uuid": UUID_A, "release_date": "01/01/2026 00:00:00",
             "floor": "6900", "listings": "7"},
            {"uuid": UUID_B, "release_date": "01/02/2026 00:00:00",
             "floor": "120", "listings": "1"}]


def _write_catalogue(path, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["uuid", "release_date", "floor", "listings"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _read_store(store):
    with open(store, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_backfill_ecrit_on_change_et_marque_done(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz")
    store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows())
    assert ph.run_backfill(cat, store, str(tmp_path)) == 0
    rows = _read_store(store)
    a = [r for r in rows if r["veve_uuid"] == UUID_A]
    b = [r for r in rows if r["veve_uuid"] == UUID_B]
    assert len(a) == 3 and len(b) == 2                      # on-change
    assert ph.load_done(str(tmp_path)) == {UUID_A, UUID_B}


def test_backfill_reprise_ne_recompte_pas(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz")
    store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows())
    ph.run_backfill(cat, store, str(tmp_path))
    n1 = len(_read_store(store))
    ph.run_backfill(cat, store, str(tmp_path))              # 2e run
    assert len(_read_store(store)) == n1                    # rien de re-ecrit


def test_recolte_sacree_item_ko_saute_les_bons_gardes(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz")
    store = str(tmp_path / "prices.csv")
    rows = _cat_rows() + [{"uuid": UUID_KO, "release_date": "01/01/2026 00:00:00",
                           "floor": "", "listings": ""}]
    _write_catalogue(cat, rows)
    assert ph.run_backfill(cat, store, str(tmp_path)) == 0
    done = ph.load_done(str(tmp_path))
    assert UUID_A in done and UUID_B in done                # bons conserves
    assert UUID_KO not in done                              # KO retente plus tard
    assert len(_read_store(store)) == 5


# --- append ----------------------------------------------------------------

def test_append_ajoute_seulement_les_changements(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz")
    store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows())
    # store deja peuple pour UUID_A a (6900,7) = valeur catalogue -> pas de doublon
    ph._ensure_store(store)
    ph.append_rows(store, [(UUID_A, "2026-06-01T00:00:00.000Z", "6900", "7")])
    ph.run_append(cat, store, store)
    rows = _read_store(store)
    a = [r for r in rows if r["veve_uuid"] == UUID_A]
    b = [r for r in rows if r["veve_uuid"] == UUID_B]
    assert len(a) == 1                                      # inchange -> rien ajoute
    assert len(b) == 1                                      # nouveau -> ajoute


def test_append_ignore_floor_vide(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz")
    store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, [{"uuid": UUID_A, "release_date": "", "floor": "",
                            "listings": ""}])
    ph.run_append(cat, store, store)
    assert not os.path.exists(store) or _read_store(store) == []
