"""Tests price_history — map veve_uuid->id tracker (/api/Nfts), filtrage
remplissage, on-change, reprise, recolte sacree, append, baselines. Zero reseau."""
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
UUID_KO = "cccccccc-9999-0000-0000-000000000000"   # a une fiche tracker mais l'API tombe
UUID_NOID = "dddddddd-0000-0000-0000-000000000000"  # aucune fiche tracker
TA, TB, TKO = "trackA", "trackB", "trackKO"          # ids INTERNES my-nft-tracker

NFTS = [{"externalReference": UUID_A, "uuid": TA},
        {"externalReference": UUID_B, "uuid": TB},
        {"externalReference": UUID_KO, "uuid": TKO}]   # UUID_NOID volontairement absent

# Historique reel simule, cle par ID TRACKER (comme l'API).
HIST = {
    TA: [("2026-01-02T01:06:00.0", 8500, 4), ("2026-01-03T02:07:00.0", 8500, 4),
         ("2026-01-10T03:08:00.0", 6900, 6), ("2026-05-01T04:09:00.0", 6900, 7)],
    TB: [("2026-02-01T00:00:00.0", 100, 1), ("2026-02-02T00:00:00.0", 120, 1)],
}


def _fake_http(url):
    q = parse_qs(urlparse(url).query)
    if "/api/Nfts" in url:
        off, lim = int(q["offset"][0]), int(q["limit"][0])
        return {"meta": {"entries_TotalAvailable": len(NFTS)},
                "resultEntries": NFTS[off:off + lim]}, ""
    guid = q["guid"][0]
    if guid == TKO:
        return None, "HTTP 500"
    frm = dt.datetime.strptime(q["fromTimeStamp"][0][:10], "%Y-%m-%d").date()
    to = dt.datetime.strptime(q["toTimeStamp"][0][:10], "%Y-%m-%d").date()
    rows = []
    for ts, floor, listings in HIST.get(guid, []):
        d = dt.datetime.strptime(ts[:10], "%Y-%m-%d").date()
        if frm <= d < to:
            rows.append({"nftUuid": guid, "createdTimestamp": ts,
                         "lowestMarketPrice": floor, "totalMarketListings": listings})
            rows.append({"nftUuid": ph.FILLER_UUID, "createdTimestamp": ts[:10] + "T12:00",
                         "lowestMarketPrice": floor, "totalMarketListings": listings})
    return rows, ""


def setup_function(_):
    ph.HTTP_GET = _fake_http
    ph.BACKOFF = 0
    ph.PAUSE = 0
    ph.WINDOW_DAYS = 120
    ph.RETRIES = 2
    ph.MIN_WINDOW_DAYS = 15
    ph.WORKERS = 2
    ph.FLUSH_ITEMS = 300
    ph.MAX_ITEMS = 0
    os.environ.pop("PH_REFRESH_MAP", None)


# --- unitaires -------------------------------------------------------------

def test_fetch_window_filtre_le_remplissage():
    rows, err = ph.fetch_window(TA, dt.date(2026, 1, 1), dt.date(2026, 1, 5))
    assert err == "" and rows and all(r["nftUuid"] == TA for r in rows)


def test_build_id_map(tmp_path):
    m, complete = ph.build_id_map(str(tmp_path))
    assert complete and m == {UUID_A: TA, UUID_B: TB, UUID_KO: TKO}
    # cache relu au 2e appel
    assert ph.build_id_map(str(tmp_path))[0] == m


def test_compress_on_change():
    obs = [{"createdTimestamp": t, "lowestMarketPrice": f, "totalMarketListings": l}
           for t, f, l in HIST[TA]]
    out = ph.compress(UUID_A, obs)
    assert [(o[2], o[3]) for o in out] == [(8500, 4), (6900, 6), (6900, 7)]
    assert all(o[0] == UUID_A for o in out)          # stocke sous veve_uuid


# --- backfill bout-en-bout -------------------------------------------------

def _cat_rows():
    return [{"uuid": UUID_A, "release_date": "01/01/2026 00:00:00", "floor": "6900",
             "listings": "7"},
            {"uuid": UUID_B, "release_date": "01/02/2026 00:00:00", "floor": "120",
             "listings": "1"}]


def _write_catalogue(path, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["uuid", "release_date", "floor", "listings"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _read_store(store):
    with open(store, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_backfill_utilise_id_tracker_et_ecrit_on_change(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows())
    assert ph.run_backfill(cat, store, str(tmp_path)) == 0
    rows = _read_store(store)
    assert len([r for r in rows if r["veve_uuid"] == UUID_A]) == 3
    assert len([r for r in rows if r["veve_uuid"] == UUID_B]) == 2
    assert ph.load_done(str(tmp_path)) == {UUID_A, UUID_B}


def test_backfill_reprise_ne_recompte_pas(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows())
    ph.run_backfill(cat, store, str(tmp_path))
    n1 = len(_read_store(store))
    ph.run_backfill(cat, store, str(tmp_path))
    assert len(_read_store(store)) == n1


def test_recolte_sacree_item_ko_saute_les_bons_gardes(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows() + [{"uuid": UUID_KO,
                     "release_date": "01/01/2026 00:00:00", "floor": "", "listings": ""}])
    assert ph.run_backfill(cat, store, str(tmp_path)) == 0
    done = ph.load_done(str(tmp_path))
    assert UUID_A in done and UUID_B in done and UUID_KO not in done
    assert len(_read_store(store)) == 5


def test_backfill_sans_fiche_tracker_est_clos(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows() + [{"uuid": UUID_NOID,
                     "release_date": "01/01/2026 00:00:00", "floor": "5", "listings": "1"}])
    ph.run_backfill(cat, store, str(tmp_path))
    done = ph.load_done(str(tmp_path))
    assert UUID_NOID in done                          # map complete -> clos (0 ligne)
    assert len([r for r in _read_store(store) if r["veve_uuid"] == UUID_NOID]) == 0


def test_id_map_depuis_catalogue_evite_le_scan(tmp_path):
    # colonne tracker_id presente -> pas de balayage /api/Nfts
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    with gzip.open(cat, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["uuid", "tracker_id", "release_date",
                                          "floor", "listings"])
        w.writeheader()
        w.writerow({"uuid": UUID_A, "tracker_id": TA,
                    "release_date": "01/01/2026 00:00:00", "floor": "6900", "listings": "7"})
    ph.run_backfill(cat, store, str(tmp_path))
    assert not os.path.exists(os.path.join(str(tmp_path), "id_map.json"))  # aucun scan
    assert len([r for r in _read_store(store) if r["veve_uuid"] == UUID_A]) == 3


# --- append ----------------------------------------------------------------

def test_append_ajoute_seulement_les_changements(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    _write_catalogue(cat, _cat_rows())
    ph._ensure_store(store)
    ph.append_rows(store, [(UUID_A, "2026-06-01T00:00:00.000Z", "6900", "7")])
    ph.run_append(cat, store, store)
    rows = _read_store(store)
    assert len([r for r in rows if r["veve_uuid"] == UUID_A]) == 1
    assert len([r for r in rows if r["veve_uuid"] == UUID_B]) == 1


def test_baselines_percentiles_et_troll_exclu(tmp_path):
    store = str(tmp_path / "prices.csv"); out = str(tmp_path / "baselines.csv.gz")
    with open(store, "w", newline="") as f:
        w = csv.writer(f); w.writerow(ph.STORE_HEADER)
        for i, floor in enumerate(range(100, 1100, 100)):
            w.writerow(("u1", f"2026-01-{i+1:02d}T00:00:00", floor, i + 1))
        w.writerow(("u1", "2026-02-01T00:00:00", 9e12, 99))
    assert ph.run_baselines(store, out) == 0
    r = list(csv.DictReader(gzip.open(out, "rt")))[0]
    assert int(r["n_points"]) == 10
    assert float(r["floor_min"]) == 100 and float(r["floor_max"]) == 1000
    assert 500 <= float(r["floor_p50"]) <= 600 and float(r["last_floor"]) == 1000


def test_backfill_saute_les_comics(tmp_path):
    cat = str(tmp_path / "catalogue.csv.gz"); store = str(tmp_path / "prices.csv")
    with gzip.open(cat, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["uuid", "kind", "release_date", "floor", "listings"])
        w.writeheader()
        w.writerow({"uuid": UUID_A, "kind": "Collectible",
                    "release_date": "01/01/2026 00:00:00", "floor": "6900", "listings": "7"})
        w.writerow({"uuid": UUID_B, "kind": "Comic",
                    "release_date": "01/02/2026 00:00:00", "floor": "120", "listings": "1"})
    ph.run_backfill(cat, store, str(tmp_path))
    done = ph.load_done(str(tmp_path))
    assert UUID_A in done and UUID_B in done          # les deux clos
    rows = _read_store(store)
    assert len([r for r in rows if r["veve_uuid"] == UUID_A]) == 3   # collectible: historique
    assert len([r for r in rows if r["veve_uuid"] == UUID_B]) == 0   # comic: rien (saute)
