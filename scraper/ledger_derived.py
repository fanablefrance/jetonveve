#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ledger_derived — portage DuckDB du ledger preda (jetonveve, 16/07/2026).

Lit le parquet produit par analytics_derived (PARQUET_OUT) + catalogue.csv.gz
(Release `catalogue`) et produit les CSV que le writer preda (ledger_writer.py)
transforme en onglets Sheet — MEMES colonnes que scraper/ledger.py prod :

  pulse.csv            _MonthlyPulse (22 col, mois + lignes annuelles)
  corner_full.csv.gz   🎯A-CORNERISATION (111 col, fiche VeveFox complete)
  profiles_full.csv.gz profils wallets (holders courants, score A VIE IMX+CC)
  wallet_size.csv      distribution wallets par taille (upsert _WalletSize)
  whales.csv           3 tops (holdings / value_floor / value_store)
  meta_ledger.csv      run + garde-fous

DECISIONS (Preda 16/07) :
  * scores collector / engagement A VIE (IMX+CC) — la prod ne scorait que CC ;
  * l'activite IMX >= 28/01/2026 (150k lignes residuelles post-migration,
    tokens morts) est EXCLUE partout, comme la prod (`day >= MIGRATION_DAY`).

Usage : python -m scraper.ledger_derived <transfers.parquet> <catalogue.csv.gz> [outdir]
Env   : RUN_DATE, DUCK_MEM (10GB), DB_PATH (ledger_work.duckdb), STEPS (all),
        UUID_COL (uuid), OG_CUTOFF (2023), PRIX_MAX (50000), WHALE_TOP (100),
        AIRDROP_MIN_MINTS (2000), AIRDROP_MINTER_RATIO (0.9),
        EXPECTED_MIN (35000000), HASH_PASSES (4).
"""
import csv
import gzip
import os
import sys
import time
from collections import defaultdict

import duckdb

ZERO = "0x0000000000000000000000000000000000000000"
ESCROW = "0xb1af72a77b9065c55cda0680b86655a79b62e42c"
BURN_SINK = "0x39e3816a8c549ec22cd1a34a8cf7034b3941d8b1"
DISTRIB = ("0x7be178ba43a9828c22997a3ec3640497d88d2fd3",
           "0xdb721de5f825fcb3d2dbe3a4778e34e43ae7c095",
           "0xc4817870a6a75704985be4f9933643a27739afc1")
DIST_L = "('" + "','".join(DISTRIB) + "')"
SYS_L = "('" + "','".join((ZERO, ESCROW, BURN_SINK) + DISTRIB) + "')"
MIG = "2026-01-28"

SCORES = ["Diamond-Hands", "Serious Collector", "Collector", "Trader",
          "Flipper", "Seasoned Flipper", "Aggressive Flipper"]
ACTIVITIES = ["Actif", "Engagé", "Somnolant", "Inactif", "Désinscrit", "Fantôme"]
ENGAGEMENTS = ["Fidèle", "Régulier", "Occasionnel", "Sporadique", "Unique"]
PROFILE_ORDER = SCORES + ["Unclassified"]
ACTIVITY_ORDER = ACTIVITIES + ["Non classé"]
QTY_ORDER = ["1", "2-10", "11-50", "51-100", "101-500", "501-1k", "1001-5k",
             "5001-10k", "10001-50k", "50001-100k", "100k+"]
VALUE_ORDER = ["≤20", "21-100", "101-500", "501-1k", "1001-5k", "5001-10k",
               "10001-50k", "50001-100k", "100001-500k", "500001-1M", "1M+"]
HOLD_ORDER = ["1", "2-5", "6-10", "11-20", "21-50", "51-100", "101-500", "500+"]
SCORE_MID = {"Diamond-Hands": 97.5, "Serious Collector": 87.5, "Collector": 70.0,
             "Trader": 50.0, "Flipper": 30.0, "Seasoned Flipper": 12.5,
             "Aggressive Flipper": 2.5}
ACTIVITY_MID = {"Actif": 95.0, "Engagé": 80.0, "Somnolant": 60.0,
                "Inactif": 40.0, "Désinscrit": 20.0, "Fantôme": 5.0}
PULSE_HEADER = ["month", "actifs", "nouveaux", "trades", "acheteurs", "vendeurs",
                "tokens_emis", "tokens_airdrop", "minters_uniques", "drops",
                "burns", "listings", "acc_net_moy", "acc_net_pos", "acc_net_neg",
                "churn_pct", "drops_vevecomics", "anciens", "og_actifs", "og_pct",
                "listeurs", "revenue_drop"]
CORNER_HEADER = (["veve_uuid", "name", "category", "circulating", "holders", "gini"]
    + [f"top{i}_{s}" for i in range(1, 11) for s in ("cnt", "pct")]
    + ["qty_dominant", "qty_dominant_pct", "vstore_dominant", "vstore_dominant_pct",
       "vfloor_dominant", "vfloor_dominant_pct", "score_dominant", "score_dominant_pct",
       "activity_dominant", "activity_dominant_pct", "engagement_dominant",
       "engagement_dominant_pct", "avg_collector", "avg_activity"]
    + [f"act_pers_{s}" for s in ACTIVITY_ORDER] + [f"act_sup_{s}" for s in ACTIVITY_ORDER]
    + [f"prof_pers_{s}" for s in PROFILE_ORDER] + [f"prof_sup_{s}" for s in PROFILE_ORDER]
    + [f"ws_pers_{s}" for s in QTY_ORDER] + [f"ws_sup_{s}" for s in QTY_ORDER]
    + [f"hold_pers_{s}" for s in HOLD_ORDER] + [f"hold_sup_{s}" for s in HOLD_ORDER]
    + ["hm_prof_act", "hm_prof_ws", "hm_act_ws"]
    # DROPPEURS DIAMANT (23/07) : par item, exemplaires encore chez leur droppeur
    # d'origine sans JAMAIS avoir ete vendus (une mise en vente non conclue = OK).
    # drop_proxy=1 -> vrai drop pre-IMX (GoChain) : le "droppeur" mesure depuis la
    # genese IMX du 14-15/12/2021, pas depuis le drop d'origine (hors-chaine).
    + ["drop_distribues", "drop_diamant", "drop_proxy"])
# Jour de genese IMX (re-mint de masse du catalogue GoChain) : un item dont le
# 1er mint on-chain tombe la est PRE-IMX -> son vrai droppeur est hors-chaine.
# (lecture directe de l'env : `E` n'est defini que plus bas.)
DROP_GENESIS = os.environ.get("DROP_GENESIS_CUTOFF") or "2021-12-15"
PROFILE_COLS = ["wallet", "holdings", "distinct_collectibles", "acquired", "sold",
                "retention", "median_hold_days", "collectorScore", "activityStatus",
                "engagementLevel", "value_store", "value_floor", "qty_bucket",
                "airdropOnly", "last_active", "listed"]
WHALE_TYPES = [("Whale Accumulatrice", "holdings"),
               ("Whale Valeur Floor", "value_floor"),
               ("Whale Valeur Store", "value_store")]

E = lambda k, d: os.environ.get(k) or d
RUN_DATE = E("RUN_DATE", time.strftime("%Y-%m-%d"))
OG_CUTOFF = E("OG_CUTOFF", "2023")
PRIX_MAX = float(E("PRIX_MAX", "50000"))
WHALE_TOP = int(E("WHALE_TOP", "100"))
AIR_MIN = int(E("AIRDROP_MIN_MINTS", "2000"))
AIR_RATIO = float(E("AIRDROP_MINTER_RATIO", "0.9"))
EXPECTED_MIN = int(E("EXPECTED_MIN", "35000000"))
PASSES = int(E("HASH_PASSES", "4"))
REVEIL_FENETRE = int(E("REVEIL_FENETRE_JOURS", "60"))
REVEIL_GAP = int(E("REVEIL_GAP_DAYS", "180"))
UUID_COL = E("UUID_COL", "uuid")


def fail(msg):
    print(f"ERREUR GARDE-FOU : {msg}")
    sys.exit(1)


def pass_range():
    """PASS_RANGE=lo-hi (sandbox 45 s) — defaut : toutes les passes."""
    pr = os.environ.get("PASS_RANGE")
    if not pr:
        return 0, PASSES - 1
    lo, hi = pr.split("-")
    return int(lo), int(hi)


def ok(c):
    return f"({c} IS NOT NULL AND {c} <> '' AND {c} NOT IN {SYS_L})"


def qty_bucket_sql(col):
    return (f"CASE WHEN {col} <= 1 THEN '1' WHEN {col} <= 10 THEN '2-10' "
            f"WHEN {col} <= 50 THEN '11-50' WHEN {col} <= 100 THEN '51-100' "
            f"WHEN {col} <= 500 THEN '101-500' WHEN {col} <= 1000 THEN '501-1k' "
            f"WHEN {col} <= 5000 THEN '1001-5k' WHEN {col} <= 10000 THEN '5001-10k' "
            f"WHEN {col} <= 50000 THEN '10001-50k' "
            f"WHEN {col} <= 100000 THEN '50001-100k' ELSE '100k+' END")


def value_bucket_sql(col):
    return (f"CASE WHEN {col} < 20 THEN '≤20' WHEN {col} < 100 THEN '21-100' "
            f"WHEN {col} < 500 THEN '101-500' WHEN {col} < 1000 THEN '501-1k' "
            f"WHEN {col} < 5000 THEN '1001-5k' WHEN {col} < 10000 THEN '5001-10k' "
            f"WHEN {col} < 50000 THEN '10001-50k' WHEN {col} < 100000 THEN '50001-100k' "
            f"WHEN {col} < 500000 THEN '100001-500k' WHEN {col} < 1000000 THEN '500001-1M' "
            f"ELSE '1M+' END")


# ── étapes ───────────────────────────────────────────────────────────────────

def step_views(con, pq):
    """Vue filtrée : l'activité IMX post-migration (tokens morts) est exclue,
    comme la prod (`day >= MIGRATION_DAY` -> continue)."""
    con.execute(f"""
        CREATE OR REPLACE VIEW pqv AS
        SELECT era, ts_utc, date_pt, kind, category,
               try_cast({UUID_COL}::VARCHAR AS UUID) AS u, edition, frm, dst, seq
        FROM read_parquet('{pq}')
        WHERE NOT (era = 'IMX' AND date_pt >= DATE '{MIG}')""")
    n = con.execute("SELECT count(*) FROM pqv").fetchone()[0]
    print(f"pqv : {n:,} lignes (IMX post-migration exclu)")
    if n < EXPECTED_MIN:
        fail(f"{n} lignes < EXPECTED_MIN {EXPECTED_MIN}")


def step_ledger(con, outdir):
    """Grand livre (méthode analytics_derived prouvée) + détentions par wallet."""
    t = time.time()
    burned = f"(dst IS NULL OR dst='' OR dst='{ZERO}' OR dst='{BURN_SINK}' OR kind='burn')"
    listed = f"(kind='listing' OR dst='{ESCROW}')"
    con.execute("""CREATE OR REPLACE TABLE last_seq AS
        SELECT max(seq) AS seq FROM pqv
        WHERE u IS NOT NULL AND edition IS NOT NULL
        GROUP BY u, edition""")
    con.execute(f"""CREATE OR REPLACE TABLE ledger AS
        SELECT p.u, p.edition,
          CASE WHEN {burned} THEN 'burned'
               WHEN coalesce(CASE WHEN {listed} THEN p.frm ELSE p.dst END,'')
                    IN {DIST_L} THEN 'stock'
               WHEN {listed} THEN 'listed' ELSE 'held' END AS status,
          CASE WHEN {burned} THEN NULL
               WHEN {listed} THEN p.frm ELSE p.dst END AS holder,
          {listed} AS is_listed
        FROM pqv p JOIN last_seq USING (seq)""")
    con.execute(f"""CREATE OR REPLACE TABLE hc AS
        SELECT u, holder AS wallet, count(*)::INTEGER AS c,
               count(*) FILTER (is_listed)::INTEGER AS listed
        FROM ledger WHERE status IN ('held','listed')
          AND holder IS NOT NULL AND holder <> '' AND holder NOT IN {SYS_L}
        GROUP BY u, holder""")
    con.execute("""CREATE OR REPLACE TABLE holdings AS
        SELECT wallet, sum(c)::BIGINT AS h, sum(listed)::BIGINT AS listed,
               count(DISTINCT u)::BIGINT AS distinct_u
        FROM hc GROUP BY wallet""")
    con.execute(f"""COPY (
        SELECT u AS veve_uuid, edition,
               CASE WHEN status IN ('held','listed')
                     AND holder IS NOT NULL AND holder <> '' AND holder NOT IN {SYS_L}
                    THEN holder ELSE '' END AS holder,
               CASE WHEN status = 'listed' THEN 1 ELSE 0 END AS listed
        FROM ledger ORDER BY veve_uuid, edition)
        TO '{outdir}/ledger_full.csv.gz' (HEADER, COMPRESSION gzip)""")
    con.execute("DROP TABLE ledger")
    con.execute("DROP TABLE last_seq")
    print(f"grand livre + hc : {time.time()-t:.0f}s —",
          con.execute("SELECT count(*), sum(c) FROM hc").fetchone())


def step_segments(con):
    """Segments de détention (méthode prouvée = replay prod au wallet près)
    -> comportement À VIE par wallet : mints, buys, sells, median_hold_days."""
    t = time.time()
    lo, hi = pass_range()
    if lo == 0:
        con.execute("""CREATE OR REPLACE TABLE starts
            (u UUID, edition INTEGER, seq BIGINT, start_ts TIMESTAMP,
             wallet VARCHAR, is_mint BOOLEAN)""")
    for k in range(lo, hi + 1):
        con.execute(f"""
        INSERT INTO starts
        WITH ev AS (
          SELECT u, edition, seq, ts_utc, frm,
            CASE WHEN (dst IS NULL OR dst='' OR dst='{ZERO}' OR dst='{BURN_SINK}'
                       OR kind='burn') THEN NULL
                 WHEN (kind='listing' OR dst='{ESCROW}') THEN frm
                 ELSE dst END AS h
          FROM pqv
          WHERE u IS NOT NULL AND edition IS NOT NULL
            AND hash(u) % {PASSES} = {k}
        ), runs AS (
          SELECT u, edition, seq, ts_utc, frm, h,
                 lag(h) OVER (PARTITION BY u, edition ORDER BY seq) AS ph
          FROM ev
        )
        SELECT u, edition, seq, ts_utc, h,
               (ph IS NULL AND (frm IS NULL OR frm='' OR frm='{ZERO}'))
        FROM runs WHERE h IS DISTINCT FROM ph""")
    if hi < PASSES - 1:
        print(f"segments : passes {lo}-{hi} faites (partiel)")
        return
    # dictionnaire wallet -> INT : la mediane des durees se calcule sur une
    # table compacte (24 M segments), pas sur des VARCHAR 42 car.
    con.execute(f"""CREATE OR REPLACE TABLE seg_wallets AS
        SELECT wallet, (row_number() OVER (ORDER BY wallet))::INTEGER AS swid
        FROM (SELECT DISTINCT wallet FROM starts
              WHERE wallet IS NOT NULL AND wallet <> '' AND wallet NOT IN {SYS_L})""")
    con.execute("""CREATE OR REPLACE TABLE seg_flat
        (swid INTEGER, is_mint BOOL, ended BOOL, dur DOUBLE)""")
    for k in range(PASSES):
        con.execute(f"""
            INSERT INTO seg_flat
            SELECT w.swid, s.is_mint, s.e IS NOT NULL,
                   epoch(s.e - s.start_ts) / 86400.0
            FROM (SELECT u, edition, seq, start_ts, wallet, is_mint,
                         lead(start_ts) OVER (PARTITION BY u, edition ORDER BY seq) AS e
                  FROM starts WHERE hash(u) % {PASSES} = {k}) s
            JOIN seg_wallets w ON s.wallet = w.wallet""")
    con.execute("DROP TABLE starts")
    con.execute(f"""CREATE OR REPLACE TABLE wallet_stats AS
        WITH a AS (
          SELECT swid,
            count(*) FILTER (is_mint)::BIGINT AS mints,
            count(*) FILTER (NOT is_mint)::BIGINT AS buys,
            count(*) FILTER (ended)::BIGINT AS sells,
            median(dur) FILTER (ended) AS median_hold
          FROM seg_flat GROUP BY swid)
        SELECT w.wallet, a.mints, a.buys, a.sells, a.median_hold
        FROM a JOIN seg_wallets w USING (swid)""")
    print(f"segments + wallet_stats : {time.time()-t:.0f}s —",
          con.execute("SELECT count(*) FROM wallet_stats").fetchone())


def step_engagement(con):
    """Semaines actives À VIE + first/last par wallet (re-mint migration exclu
    de last/weeks, compté dans first — comme la prod)."""
    t = time.time()
    lo, hi = pass_range()
    if lo == 0:
        con.execute("""CREATE OR REPLACE TABLE engagement
            (wallet VARCHAR, first_day DATE, last_day DATE, n_weeks INTEGER,
             veille DATE, dedans DATE)""")
    for k in range(lo, hi + 1):
        con.execute(f"""
        INSERT INTO engagement
        WITH ev AS (
          SELECT date_pt AS d, unnest([frm, dst]) AS w,
                 (date_pt = DATE '{MIG}' AND (frm IS NULL OR frm = '' OR frm IN {SYS_L})) AS remint
          FROM pqv
        )
        SELECT w, min(d), max(d) FILTER (NOT remint),
               count(DISTINCT CASE WHEN NOT remint
                     THEN isoyear(d)*100 + weekofyear(d) END),
               max(d) FILTER (NOT remint AND d <  DATE '{RUN_DATE}' - INTERVAL ({REVEIL_FENETRE}) DAY),
               min(d) FILTER (NOT remint AND d >= DATE '{RUN_DATE}' - INTERVAL ({REVEIL_FENETRE}) DAY)
        FROM ev
        WHERE w IS NOT NULL AND w <> '' AND w NOT IN {SYS_L} AND hash(w) % {PASSES} = {k}
        GROUP BY w""")
    print(f"engagement : {time.time()-t:.0f}s —",
          con.execute("SELECT count(*) FROM engagement").fetchone())


def step_catalogue(con, cat_path):
    """Prix store/floor par uuid (règles prod : 0 < p <= PRIX_MAX,
    floor aberrant ou absent -> le store prend le relais)."""
    con.execute(f"""CREATE OR REPLACE TABLE cat AS
        SELECT try_cast(lower(trim(uuid)) AS UUID) AS u, any_value(name) AS name,
               any_value(kind) AS kind,
               max(CASE WHEN try_cast(store_price AS DOUBLE) > 0
                         AND try_cast(store_price AS DOUBLE) <= {PRIX_MAX}
                    THEN try_cast(store_price AS DOUBLE) END) AS store,
               max(CASE WHEN try_cast(floor AS DOUBLE) > 0
                         AND try_cast(floor AS DOUBLE) <= {PRIX_MAX}
                    THEN try_cast(floor AS DOUBLE) END) AS floor
        FROM read_csv('{cat_path}', all_varchar=true)
        WHERE try_cast(lower(trim(uuid)) AS UUID) IS NOT NULL
        GROUP BY try_cast(lower(trim(uuid)) AS UUID)""")
    n, ns, nf = con.execute("""SELECT count(*), count(store), count(floor)
                               FROM cat""").fetchone()
    print(f"catalogue : {n:,} items, {ns:,} store, {nf:,} floor")
    if n < 15000:
        fail(f"catalogue {n} items < 15000")


def step_attrs(con):
    """Attributs par HOLDER courant : score À VIE, activité, engagement,
    buckets qty/valeur — mêmes règles que ledger.py prod."""
    t = time.time()
    con.execute(f"""CREATE OR REPLACE TABLE wallet_values AS
        SELECT h.wallet,
          coalesce(sum(h.c * c.store), 0)                 AS value_store,
          coalesce(sum(h.c * coalesce(c.floor, c.store)), 0) AS value_floor
        FROM hc h LEFT JOIN cat c USING (u)
        GROUP BY h.wallet""")
    con.execute(f"""CREATE OR REPLACE TABLE attrs AS
        SELECT hd.wallet, hd.h AS holdings, hd.distinct_u, hd.listed,
          coalesce(ws.mints, 0) AS mints, coalesce(ws.buys, 0) AS buys,
          coalesce(ws.sells, 0) AS sells, ws.median_hold,
          CASE WHEN coalesce(ws.mints,0) + coalesce(ws.buys,0) < 3 THEN 'n/a'
            ELSE (CASE least(6 + CASE WHEN ws.median_hold IS NOT NULL AND ws.median_hold < 7 THEN 0 ELSE 0 END,
              CASE WHEN r >= 0.95 THEN 0 WHEN r >= 0.75 THEN 1 WHEN r >= 0.50 THEN 2
                   WHEN r >= 0.30 THEN 3 WHEN r >= 0.15 THEN 4 WHEN r >= 0.05 THEN 5
                   ELSE 6 END
              + CASE WHEN ws.median_hold IS NOT NULL AND ws.median_hold < 7
                      AND (CASE WHEN r >= 0.95 THEN 0 WHEN r >= 0.75 THEN 1
                                WHEN r >= 0.50 THEN 2 WHEN r >= 0.30 THEN 3
                                WHEN r >= 0.15 THEN 4 WHEN r >= 0.05 THEN 5
                                ELSE 6 END) < 4 THEN 1 ELSE 0 END)
              WHEN 0 THEN 'Diamond-Hands' WHEN 1 THEN 'Serious Collector'
              WHEN 2 THEN 'Collector' WHEN 3 THEN 'Trader' WHEN 4 THEN 'Flipper'
              WHEN 5 THEN 'Seasoned Flipper' ELSE 'Aggressive Flipper' END)
          END AS sc,
          CASE WHEN e.last_day IS NULL THEN ''
               WHEN dj <= 7 THEN 'Actif'      WHEN dj <= 30 THEN 'Engagé'
               WHEN dj <= 90 THEN 'Somnolant' WHEN dj <= 180 THEN 'Inactif'
               WHEN dj <= 365 THEN 'Désinscrit' ELSE 'Fantôme' END AS ac,
          CASE WHEN e.wallet IS NULL OR coalesce(e.n_weeks, 0) = 0 THEN 'n/a'
               WHEN e.n_weeks = 1 THEN 'Unique'
               WHEN rw >= 0.5 THEN 'Fidèle' WHEN rw >= 0.25 THEN 'Régulier'
               WHEN rw >= 0.10 THEN 'Occasionnel' ELSE 'Sporadique' END AS en,
          {qty_bucket_sql('hd.h')} AS qb,
          {value_bucket_sql('coalesce(v.value_store, 0)')} AS vsb,
          {value_bucket_sql('coalesce(v.value_floor, 0)')} AS vfb,
          round(coalesce(v.value_store, 0), 2) AS value_store,
          round(coalesce(v.value_floor, 0), 2) AS value_floor,
          e.first_day, e.last_day
        FROM holdings hd
        LEFT JOIN wallet_stats ws USING (wallet)
        LEFT JOIN engagement e ON e.wallet = hd.wallet
        LEFT JOIN wallet_values v ON v.wallet = hd.wallet,
        LATERAL (SELECT
          coalesce(hd.h, 0)::DOUBLE / nullif(coalesce(ws.mints,0)+coalesce(ws.buys,0), 0) AS r,
          date_diff('day', e.last_day, DATE '{RUN_DATE}') AS dj,
          e.n_weeks::DOUBLE / greatest(1,
            date_diff('day', e.first_day, DATE '{RUN_DATE}') // 7 + 1) AS rw) x""")
    print(f"attrs : {time.time()-t:.0f}s —",
          con.execute("SELECT count(*) FROM attrs").fetchone())


def _dominant(dist, order):
    total = sum(dist.values())
    if not total:
        return "", 0
    best = max(order, key=lambda k: dist.get(k, 0))
    return best, round(100.0 * dist.get(best, 0) / total, 1)


def step_corner(con, outdir):
    """🎯A-CORNERISATION 111 colonnes — agrégats SQL compacts puis assemblage
    Python (boucle identique à ledger.py prod)."""
    t = time.time()
    con.execute("""CREATE OR REPLACE TABLE jt AS
        SELECT h.u, h.wallet, h.c, a.sc, a.ac, a.en, a.qb, a.vsb, a.vfb,
          CASE WHEN h.c <= 1 THEN '1' WHEN h.c <= 5 THEN '2-5'
               WHEN h.c <= 10 THEN '6-10' WHEN h.c <= 20 THEN '11-20'
               WHEN h.c <= 50 THEN '21-50' WHEN h.c <= 100 THEN '51-100'
               WHEN h.c <= 500 THEN '101-500' ELSE '500+' END AS hb
        FROM hc h JOIN attrs a USING (wallet)""")
    con.execute("CREATE OR REPLACE TABLE c_base AS SELECT u, sum(c)::BIGINT AS circ, count(*)::BIGINT AS holders FROM jt GROUP BY u")
    con.execute("""CREATE OR REPLACE TABLE c_gini AS
        WITH r AS (SELECT u, c, row_number() OVER (PARTITION BY u ORDER BY c) AS rn,
                          count(*) OVER (PARTITION BY u) AS n,
                          sum(c) OVER (PARTITION BY u) AS s FROM hc)
        SELECT u, round((2.0*sum(rn*c))/(any_value(n)*any_value(s))
                        - (any_value(n)+1.0)/any_value(n), 4) AS g
        FROM r GROUP BY u""")
    con.execute("""CREATE OR REPLACE TABLE c_top10 AS
        SELECT u, c, rn FROM (SELECT u, c, row_number() OVER
          (PARTITION BY u ORDER BY c DESC, wallet) AS rn FROM hc) WHERE rn <= 10""")
    for name, dim in [("g_ac", "ac"), ("g_sc", "sc"), ("g_qb", "qb"),
                      ("g_hb", "hb"), ("g_en", "en"), ("g_vs", "vsb"), ("g_vf", "vfb")]:
        con.execute(f"""CREATE OR REPLACE TABLE {name} AS
            SELECT u, {dim} AS k, count(*)::BIGINT AS pers, sum(c)::BIGINT AS sup
            FROM jt GROUP BY u, {dim}""")
    for name, d1, d2 in [("h_pa", "sc", "ac"), ("h_pw", "sc", "qb"), ("h_aw", "ac", "qb")]:
        con.execute(f"""CREATE OR REPLACE TABLE {name} AS
            SELECT u, {d1} AS k1, {d2} AS k2, sum(c)::BIGINT AS sup
            FROM jt GROUP BY u, {d1}, {d2}""")

    # DROPPEURS DIAMANT (23/07) — par edition (token) : le droppeur = 1er
    # destinataire NON systeme ; "jamais vendu" (mise en vente non conclue OK) =
    # le token n'a JAMAIS touche d'autre wallet reel que lui (min = max sur les
    # dst non systeme), et n'est pas brule. Trace les DEUX eres (IMX pre-migration
    # + CollectChain) via pqv, donc le re-mint de migration ne compte pas comme
    # une vente. proxy = 1er mint on-chain a la genese IMX -> vrai drop pre-IMX.
    con.execute(f"""CREATE OR REPLACE TABLE c_drop AS
        WITH tok AS (
          SELECT u, edition,
            min(dst) FILTER (WHERE dst IS NOT NULL AND dst <> '' AND dst NOT IN {SYS_L}) AS ns_min,
            max(dst) FILTER (WHERE dst IS NOT NULL AND dst <> '' AND dst NOT IN {SYS_L}) AS ns_max,
            bool_or(dst = '{ZERO}' OR dst = '{BURN_SINK}') AS has_burn,
            min(date_pt) FILTER (WHERE kind IN ('mint','vault_mint')) AS first_mint
          FROM pqv WHERE u IS NOT NULL AND edition IS NOT NULL
          GROUP BY u, edition)
        SELECT u,
          count(*) FILTER (WHERE ns_min IS NOT NULL)::BIGINT AS distr,
          count(*) FILTER (WHERE ns_min IS NOT NULL AND ns_min = ns_max
                           AND NOT has_burn)::BIGINT AS diam,
          max(CASE WHEN first_mint <= DATE '{DROP_GENESIS}' THEN 1 ELSE 0 END)::INTEGER AS proxy
        FROM tok GROUP BY u""")

    cat = {u: (nm or "", kd or "") for u, nm, kd in
           con.execute("SELECT u::VARCHAR, name, kind FROM cat").fetchall()}
    base = {u: (int(c), int(h)) for u, c, h in
            con.execute("SELECT u::VARCHAR, circ, holders FROM c_base").fetchall()}
    gini = dict(con.execute("SELECT u::VARCHAR, g FROM c_gini").fetchall())
    tops = defaultdict(dict)
    for u, c, rn in con.execute("SELECT u::VARCHAR, c, rn FROM c_top10").fetchall():
        tops[u][rn] = int(c)

    def load2(tname):
        d = defaultdict(dict)
        for u, k, pers, sup in con.execute(
                f"SELECT u::VARCHAR, k, pers, sup FROM {tname}").fetchall():
            d[u][k] = (int(pers), int(sup))
        return d
    g_ac, g_sc, g_qb, g_hb, g_en, g_vs, g_vf = (load2(n) for n in
        ("g_ac", "g_sc", "g_qb", "g_hb", "g_en", "g_vs", "g_vf"))

    def load3(tname):
        d = defaultdict(dict)
        for u, a, b, sup in con.execute(
                f"SELECT u::VARCHAR, k1, k2, sup FROM {tname}").fetchall():
            d[u][(a, b)] = int(sup)
        return d
    h_pa, h_pw, h_aw = load3("h_pa"), load3("h_pw"), load3("h_aw")
    drop_st = {u: (int(d), int(m), int(p)) for u, d, m, p in
               con.execute("SELECT u::VARCHAR, distr, diam, proxy FROM c_drop").fetchall()}

    rows, bad = [], 0
    ac_keys = ACTIVITIES + [""]
    sc_keys = SCORES + ["n/a"]
    for u, (circ, holders) in base.items():
        nm, kd = cat.get(u, ("", ""))
        row = [u, nm, kd, circ, holders, float(gini.get(u) or 0.0)]
        tt = tops[u]
        for i in range(1, 11):
            if i in tt:
                row += [tt[i], round(100.0 * tt[i] / circ, 2) if circ else 0]
            else:
                row += ["", ""]
        sup = lambda g: {k: v[1] for k, v in g[u].items()}
        b_qty, b_sc, b_ac = sup(g_qb), sup(g_sc), sup(g_ac)
        b_en, b_vs, b_vf = sup(g_en), sup(g_vs), sup(g_vf)
        for dist, order in ((b_qty, QTY_ORDER), (b_vs, VALUE_ORDER),
                            (b_vf, VALUE_ORDER), (b_sc, SCORES + ["n/a"]),
                            (b_ac, ACTIVITIES), (b_en, ENGAGEMENTS + ["n/a"])):
            d, p = _dominant(dist, order)
            row += [d, p]
        sum_c = sum(SCORE_MID[k] * v for k, v in b_sc.items() if k in SCORE_MID)
        wt_c = sum(v for k, v in b_sc.items() if k in SCORE_MID)
        sum_a = sum(ACTIVITY_MID[k] * v for k, v in b_ac.items() if k in ACTIVITY_MID)
        wt_a = sum(v for k, v in b_ac.items() if k in ACTIVITY_MID)
        row += [round(sum_c / wt_c, 1) if wt_c else "",
                round(sum_a / wt_a, 1) if wt_a else ""]
        row += [g_ac[u].get(k, (0, 0))[0] for k in ac_keys]
        row += [g_ac[u].get(k, (0, 0))[1] for k in ac_keys]
        row += [g_sc[u].get(k, (0, 0))[0] for k in sc_keys]
        row += [g_sc[u].get(k, (0, 0))[1] for k in sc_keys]
        row += [g_qb[u].get(k, (0, 0))[0] for k in QTY_ORDER]
        row += [g_qb[u].get(k, (0, 0))[1] for k in QTY_ORDER]
        row += [g_hb[u].get(k, (0, 0))[0] for k in HOLD_ORDER]
        row += [g_hb[u].get(k, (0, 0))[1] for k in HOLD_ORDER]
        pa, pw, aw = h_pa[u], h_pw[u], h_aw[u]
        km = lambda a: "" if a == "Non classé" else a
        pm = lambda p_: "n/a" if p_ == "Unclassified" else p_
        row += ["|".join(";".join(str(pa.get((pm(pp), km(a)), 0))
                         for a in ACTIVITY_ORDER) for pp in PROFILE_ORDER),
                "|".join(";".join(str(pw.get((pm(pp), q), 0))
                         for q in QTY_ORDER) for pp in PROFILE_ORDER),
                "|".join(";".join(str(aw.get((km(a), q), 0))
                         for q in QTY_ORDER) for a in ACTIVITY_ORDER)]
        if sum(g_ac[u].get(k, (0, 0))[0] for k in ac_keys) != holders:
            bad += 1
        if sum(g_ac[u].get(k, (0, 0))[1] for k in ac_keys) != circ:
            bad += 1
        dd, di, dp = drop_st.get(u, (0, 0, 0))
        row += [dd, di, dp]                    # droppeurs / diamant / proxy
        rows.append(row)
    if bad:
        fail(f"corner : {bad} invariant(s) pers/sup casse(s)")
    rows.sort(key=lambda r: -r[3])
    with gzip.open(f"{outdir}/corner_full.csv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CORNER_HEADER)
        w.writerows(rows)
    print(f"corner : {len(rows)} items, invariants OK, {time.time()-t:.0f}s")
    return {u: b for u, b in base.items()}


def step_pulse_tables(con):
    """Tables du pulse : escrow IMX, (mois × wallet), compteurs, airdrops,
    first_month (recul GoChain via data/gochain_wallets.csv du repo)."""
    t = time.time()
    lo, hi = pass_range()
    if lo == 0:
        _pulse_tables_init(con)
    _pulse_mw_passes(con, lo, hi)
    if hi < PASSES - 1:
        print(f"pulse_tables : passes mw {lo}-{hi} faites (partiel)")
        return
    _pulse_tables_final(con)
    print(f"tables pulse : {time.time()-t:.0f}s — mw",
          con.execute("SELECT count(*) FROM mw").fetchone())


def _pulse_tables_init(con):
    con.execute(f"""CREATE OR REPLACE TABLE imx_sellers AS
        WITH e AS (
          SELECT u, edition AS ed, seq, frm, dst, (frm = '{ESCROW}') AS is_out
          FROM pqv
          WHERE era='IMX' AND kind='transfer' AND (dst='{ESCROW}' OR frm='{ESCROW}')
            AND frm <> '{ZERO}' AND dst NOT IN ('{ZERO}','{BURN_SINK}')
        ), g AS (
          SELECT *, sum(CASE WHEN is_out THEN 1 ELSE 0 END) OVER
            (PARTITION BY coalesce(u::VARCHAR,''), ed ORDER BY seq
             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS grp
          FROM e
        ), dep AS (
          SELECT coalesce(u::VARCHAR,'') AS u, ed, coalesce(grp,0) AS grp,
                 arg_max(frm, seq) AS seller
          FROM g WHERE NOT is_out AND {ok('frm')}
          GROUP BY 1, 2, 3
        )
        SELECT o.seq, coalesce(d.seller,'') AS seller
        FROM g o LEFT JOIN dep d
          ON coalesce(o.u::VARCHAR,'')=d.u AND o.ed=d.ed AND coalesce(o.grp,0)=d.grp
        WHERE o.is_out""")
    con.execute("CREATE OR REPLACE TABLE wallet_ids AS SELECT wallet, (row_number() OVER (ORDER BY wallet))::INTEGER AS wid FROM engagement")
    con.execute("""CREATE OR REPLACE TABLE month_ids AS
        SELECT m, (row_number() OVER (ORDER BY m))::SMALLINT AS mid
        FROM (SELECT DISTINCT strftime(date_pt, '%Y-%m') AS m FROM pqv)""")
    con.execute("""CREATE OR REPLACE TABLE mw (mid SMALLINT, wid INTEGER,
        net INTEGER, minter BOOL, buyer BOOL, seller BOOL, lister BOOL, netted BOOL)""")


def _pulse_mw_passes(con, lo, hi):
    st = lambda w, d, g: f"[struct_pack(w := {w}, d := {d}::TINYINT, g := {g}::TINYINT)]"
    EL = "[]::STRUCT(w VARCHAR, d TINYINT, g TINYINT)[]"
    cc_sel = f"""
      SELECT m, s.w AS w, s.d AS d, s.g AS g FROM (
        SELECT m, unnest(CASE
          WHEN k='mint' THEN (CASE WHEN day <> DATE '{MIG}' AND {ok('dst')}
                              THEN {st('dst', 1, 1)} ELSE {EL} END)
          WHEN k='market' THEN list_concat(
              CASE WHEN {ok('dst')} THEN {st('dst', 1, 2)} ELSE {EL} END,
              CASE WHEN {ok('frm')} THEN {st('frm', -1, 3)} ELSE {EL} END)
          WHEN k='burn' THEN (CASE WHEN {ok('frm')} THEN {st('frm', -1, 0)} ELSE {EL} END)
          WHEN k='system_transfer' THEN list_concat(
              CASE WHEN {ok('dst')} THEN {st('dst', 1, 0)} ELSE {EL} END,
              CASE WHEN {ok('frm')} THEN {st('frm', -1, 0)} ELSE {EL} END)
          WHEN k='listing' THEN (CASE WHEN {ok('frm')} THEN {st('frm', 0, 4)} ELSE {EL} END)
          ELSE {EL} END) AS s
        FROM (SELECT strftime(date_pt, '%Y-%m') AS m, date_pt AS day, frm, dst,
                CASE WHEN kind IN ('market','system_transfer')
                      AND (frm IN {DIST_L} OR dst IN {DIST_L})
                     THEN 'system_transfer' ELSE kind END AS k
              FROM pqv WHERE era = 'CollectChain'))"""
    imx_sel = f"""
      SELECT m, s.w AS w, s.d AS d, s.g AS g FROM (
        SELECT m, unnest(CASE
          WHEN kind='mint' OR frm='{ZERO}' THEN
              (CASE WHEN {ok('dst')} THEN {st('dst', 1, 1)} ELSE {EL} END)
          WHEN dst IN ('{ZERO}','{BURN_SINK}') THEN
              (CASE WHEN {ok('frm')} THEN {st('frm', -1, 0)} ELSE {EL} END)
          WHEN dst = '{ESCROW}' THEN {EL}
          WHEN frm = '{ESCROW}' THEN (CASE
              WHEN seller = dst THEN {EL}
              WHEN {ok('dst')} THEN list_concat({st('dst', 1, 2)},
                   CASE WHEN seller <> '' THEN {st('seller', -1, 3)} ELSE {EL} END)
              ELSE {EL} END)
          WHEN frm IN {DIST_L} OR dst IN {DIST_L} THEN list_concat(
              CASE WHEN {ok('dst')} THEN {st('dst', 1, 0)} ELSE {EL} END,
              CASE WHEN {ok('frm')} THEN {st('frm', -1, 0)} ELSE {EL} END)
          ELSE list_concat(
              CASE WHEN {ok('dst')} THEN {st('dst', 1, 2)} ELSE {EL} END,
              CASE WHEN {ok('frm')} THEN {st('frm', -1, 3)} ELSE {EL} END)
        END) AS s
        FROM (SELECT strftime(x.date_pt, '%Y-%m') AS m, x.frm, x.dst, x.kind,
                     coalesce(se.seller, '') AS seller
              FROM pqv x LEFT JOIN imx_sellers se ON x.seq = se.seq
              WHERE x.era = 'IMX'))"""
    for k in range(lo, hi + 1):
        con.execute(f"""
          INSERT INTO mw
          SELECT mi.mid, wi.wid, sum(t.d)::INTEGER,
                 bool_or(t.g=1), bool_or(t.g=2), bool_or(t.g=3),
                 bool_or(t.g=4), bool_or(t.g<>4)
          FROM ({cc_sel} UNION ALL {imx_sel}) t
          JOIN wallet_ids wi ON t.w = wi.wallet
          JOIN month_ids mi ON t.m = mi.m
          WHERE hash(t.w) % {PASSES} = {k}
          GROUP BY mi.mid, wi.wid""")


def _pulse_tables_final(con):
    con.execute(f"""CREATE OR REPLACE TABLE cnt_cc AS
        SELECT strftime(date_pt,'%Y-%m') AS m,
          count(*) FILTER (k='mint' AND date_pt <> DATE '{MIG}' AND {ok('dst')}) AS mints,
          count(*) FILTER (k='market') AS market,
          count(*) FILTER (k='burn') AS burns,
          count(*) FILTER (k='listing') AS listings
        FROM (SELECT date_pt, frm, dst,
                CASE WHEN kind IN ('market','system_transfer')
                      AND (frm IN {DIST_L} OR dst IN {DIST_L})
                     THEN 'system_transfer' ELSE kind END AS k
              FROM pqv WHERE era='CollectChain')
        GROUP BY 1""")
    nm = f"NOT (x.kind='mint' OR x.frm='{ZERO}')"
    con.execute(f"""CREATE OR REPLACE TABLE cnt_imx AS
        SELECT strftime(x.date_pt,'%Y-%m') AS m,
          count(*) FILTER ((x.kind='mint' OR x.frm='{ZERO}') AND {ok('x.dst')}) AS mints,
          count(*) FILTER ({nm} AND x.dst IN ('{ZERO}','{BURN_SINK}')) AS burns,
          count(*) FILTER ({nm} AND x.dst NOT IN ('{ZERO}','{BURN_SINK}')
                           AND x.dst='{ESCROW}') AS listings,
          count(*) FILTER ({nm} AND x.dst NOT IN ('{ZERO}','{BURN_SINK}')
                           AND x.dst<>'{ESCROW}' AND x.frm='{ESCROW}'
                           AND coalesce(se.seller,'') <> x.dst AND {ok('x.dst')}) AS market_esc,
          count(*) FILTER ({nm} AND x.dst NOT IN ('{ZERO}','{BURN_SINK}')
                           AND x.dst<>'{ESCROW}' AND x.frm<>'{ESCROW}'
                           AND NOT (x.frm IN {DIST_L} OR x.dst IN {DIST_L})) AS market_direct
        FROM pqv x LEFT JOIN imx_sellers se ON x.seq = se.seq
        WHERE x.era='IMX' GROUP BY 1""")
    con.execute(f"""CREATE OR REPLACE TABLE cc_mints AS
        SELECT date_pt AS day, strftime(date_pt,'%Y-%m') AS m, u, dst
        FROM pqv WHERE era='CollectChain' AND kind='mint'
          AND date_pt <> DATE '{MIG}' AND {ok('dst')}""")
    con.execute(f"""CREATE OR REPLACE TABLE airdrops AS
        SELECT day, u, count(*) AS mints FROM cc_mints GROUP BY day, u
        HAVING count(*) >= {AIR_MIN}
           AND count(DISTINCT dst) >= {AIR_RATIO} * count(*)""")
    con.execute("""CREATE OR REPLACE TABLE air_wallets AS
        SELECT c.dst AS wallet, count(*) AS air_mints
        FROM cc_mints c JOIN airdrops a ON c.day=a.day AND c.u=a.u GROUP BY c.dst""")
    con.execute("CREATE OR REPLACE TABLE mint_month_uuid AS SELECT m, u, count(*) AS cnt FROM cc_mints GROUP BY m, u")
    con.execute(f"""CREATE OR REPLACE TABLE uuid_first_mint AS
        SELECT u, min(date_pt) AS day, arg_min(coalesce(category,''), date_pt) AS cat
        FROM pqv WHERE era='CollectChain' AND kind='mint' AND date_pt <> DATE '{MIG}'
          AND u IS NOT NULL
        GROUP BY u""")
    gochain = E("GOCHAIN_CSV", "data/gochain_wallets.csv")
    goc = ""
    if os.path.exists(gochain):
        goc = f"""LEFT JOIN (SELECT lower(trim(wallet)) AS wallet,
              min(substr(trim(first_seen),1,7)) AS fs
            FROM read_csv('{gochain}', all_varchar=true)
            WHERE length(trim(first_seen)) >= 7 GROUP BY 1) g USING (wallet)"""
    else:
        print("⚠️ data/gochain_wallets.csv absent — anciennete GoChain non fusionnee.")
    con.execute(f"""CREATE OR REPLACE TABLE first_month AS
        SELECT w.wid, w.wallet,
               least({"coalesce(g.fs,'9999')" if goc else "'9999'"}, mi.m) AS fm
        FROM (SELECT wid, min(mid) AS mid FROM mw WHERE netted GROUP BY wid) f
        JOIN wallet_ids w USING (wid) JOIN month_ids mi USING (mid) {goc}""")


def step_pulse_csv(con, outdir):
    """Assemblage des 22 colonnes (mois + lignes annuelles), revenue_drop
    valorise au prix store ACTUEL (airdrops deduits) — ere CC seulement."""
    t = time.time()
    months = dict(con.execute("SELECT mid, m FROM month_ids ORDER BY mid").fetchall())
    Z = dict(actifs=0, buyers=0, sellers=0, minters=0, listers=0,
             net_sum=0, pos=0, neg=0)
    agg = {m: dict(Z) for m in months.values()}
    for mid, act, buy, sel, mnt, lst, ns, pos, neg in con.execute("""
        SELECT mid, count(*) FILTER (netted), count(*) FILTER (buyer),
               count(*) FILTER (seller), count(*) FILTER (minter),
               count(*) FILTER (lister), coalesce(sum(net) FILTER (netted),0),
               count(*) FILTER (netted AND net>0), count(*) FILTER (netted AND net<0)
        FROM mw GROUP BY mid""").fetchall():
        agg[months[mid]] = dict(actifs=act, buyers=buy, sellers=sel, minters=mnt,
                                listers=lst, net_sum=ns, pos=pos, neg=neg)
    cc = {m: r for m, *r in con.execute(
        "SELECT m, mints, market, burns, listings FROM cnt_cc").fetchall()}
    imx = {m: r for m, *r in con.execute(
        "SELECT m, mints, burns, listings, market_esc, market_direct FROM cnt_imx").fetchall()}
    churn_gone = dict(con.execute("""
        WITH p AS (SELECT DISTINCT mid, wid FROM mw WHERE netted)
        SELECT a.mid + 1, count(*) FROM p a
        LEFT JOIN p b ON b.wid=a.wid AND b.mid=a.mid+1
        WHERE b.wid IS NULL GROUP BY a.mid + 1""").fetchall())
    anciens_m = dict(con.execute("""
        SELECT mid, count(*) FROM
          (SELECT wid, mid, lag(mid) OVER (PARTITION BY wid ORDER BY mid) pm
           FROM (SELECT DISTINCT mid, wid FROM mw WHERE netted))
        WHERE pm IS NOT NULL AND mid - pm > 6 GROUP BY mid""").fetchall())
    anciens_y = dict(con.execute("""
        SELECT substr(m,1,4), count(DISTINCT wid) FROM
          (SELECT wid, mid, lag(mid) OVER (PARTITION BY wid ORDER BY mid) pm
           FROM (SELECT DISTINCT mid, wid FROM mw WHERE netted)) x
        JOIN month_ids USING (mid)
        WHERE pm IS NOT NULL AND mid - pm > 6 GROUP BY 1""").fetchall())
    og_m = dict(con.execute(f"""
        SELECT mid, count(*) FROM (SELECT DISTINCT mid, wid FROM mw WHERE netted) p
        JOIN first_month f USING (wid) WHERE f.fm < '{OG_CUTOFF}' GROUP BY mid""").fetchall())
    og_y = dict(con.execute(f"""
        SELECT substr(m,1,4), count(DISTINCT wid) FROM mw JOIN month_ids USING (mid)
        JOIN first_month f USING (wid)
        WHERE netted AND f.fm < '{OG_CUTOFF}' GROUP BY 1""").fetchall())
    new_m = dict(con.execute("SELECT fm, count(*) FROM first_month GROUP BY fm").fetchall())
    new_y = dict(con.execute("SELECT substr(fm,1,4), count(*) FROM first_month GROUP BY 1").fetchall())
    drops_m, wed_m = {}, {}
    for u, day, cat_ in con.execute("SELECT u::VARCHAR, day, cat FROM uuid_first_mint").fetchall():
        m = day.strftime("%Y-%m")
        if day.weekday() == 2 and cat_ == "comic":
            wed_m[m] = wed_m.get(m, 0) + 1
        else:
            drops_m[m] = drops_m.get(m, 0) + 1
    air_m = dict(con.execute(
        "SELECT strftime(day,'%Y-%m'), sum(mints)::BIGINT FROM airdrops GROUP BY 1").fetchall())
    rev_m = dict(con.execute("""
        SELECT mm.m, round(sum(greatest(0, mm.cnt - coalesce(a.air,0)) * c.store))::BIGINT
        FROM mint_month_uuid mm
        JOIN cat c USING (u)
        LEFT JOIN (SELECT strftime(day,'%Y-%m') AS m, u, sum(mints) AS air
                   FROM airdrops GROUP BY 1, 2) a ON a.m=mm.m AND a.u=mm.u
        WHERE c.store IS NOT NULL GROUP BY mm.m""").fetchall())
    agg_y = {}
    for y, act, buy, sel, mnt, lst, ns, pos, neg in con.execute("""
        WITH w AS (SELECT substr(m,1,4) AS y, wid, bool_or(minter) minter,
                          bool_or(buyer) buyer, bool_or(seller) seller,
                          bool_or(lister) lister, bool_or(netted) netted,
                          sum(CASE WHEN netted THEN net ELSE 0 END) net
                   FROM mw JOIN month_ids USING (mid) GROUP BY 1, wid)
        SELECT y, count(*) FILTER (netted), count(*) FILTER (buyer),
               count(*) FILTER (seller), count(*) FILTER (minter),
               count(*) FILTER (lister), coalesce(sum(net) FILTER (netted),0),
               count(*) FILTER (netted AND net>0), count(*) FILTER (netted AND net<0)
        FROM w GROUP BY y""").fetchall():
        agg_y[y] = dict(actifs=act, buyers=buy, sellers=sel, minters=mnt,
                        listers=lst, net_sum=ns, pos=pos, neg=neg)
    churn_gone_y = dict(con.execute("""
        WITH p AS (SELECT DISTINCT substr(m,1,4)::INT AS y, wid
                   FROM mw JOIN month_ids USING (mid) WHERE netted)
        SELECT a.y + 1, count(*) FROM p a
        LEFT JOIN p b ON b.wid=a.wid AND b.y=a.y+1
        WHERE b.wid IS NULL GROUP BY a.y + 1""").fetchall())

    rows = []
    prev_actifs = None
    for mid in sorted(months):
        m = months[mid]
        a = agg[m]
        c = cc.get(m, [0, 0, 0, 0])
        i = imx.get(m, [0, 0, 0, 0, 0])
        churn = "" if prev_actifs in (None, 0) else round(
            100.0 * churn_gone.get(mid, 0) / prev_actifs, 1)
        og = og_m.get(mid, 0)
        rows.append([m, a["actifs"], new_m.get(m, 0), c[1] + i[3] + i[4],
                     a["buyers"], a["sellers"], c[0] + i[0], air_m.get(m, 0),
                     a["minters"], drops_m.get(m, 0), c[2] + i[1], c[3] + i[2],
                     round(a["net_sum"] / a["actifs"], 2) if a["actifs"] else 0,
                     a["pos"], a["neg"], churn, wed_m.get(m, 0),
                     anciens_m.get(mid, 0), og,
                     round(100.0 * og / a["actifs"], 1) if a["actifs"] else "",
                     a["listers"], rev_m.get(m, "")])
        prev_actifs = a["actifs"]
    prev_y_act = None
    for y in sorted({m[:4] for m in months.values()}):
        a = agg_y[y]
        my = [m for m in months.values() if m[:4] == y]
        churn = "" if prev_y_act in (None, 0) else round(
            100.0 * churn_gone_y.get(int(y), 0) / prev_y_act, 1)
        og = og_y.get(y, 0)
        rev_y = sum(v for k, v in rev_m.items() if k[:4] == y)
        rows.append([y, a["actifs"], new_y.get(y, 0),
                     sum(cc.get(m, [0]*4)[1] + imx.get(m, [0]*5)[3]
                         + imx.get(m, [0]*5)[4] for m in my),
                     a["buyers"], a["sellers"],
                     sum(cc.get(m, [0]*4)[0] + imx.get(m, [0]*5)[0] for m in my),
                     sum(v for k, v in air_m.items() if k[:4] == y),
                     a["minters"],
                     sum(v for k, v in drops_m.items() if k[:4] == y),
                     sum(cc.get(m, [0]*4)[2] + imx.get(m, [0]*5)[1] for m in my),
                     sum(cc.get(m, [0]*4)[3] + imx.get(m, [0]*5)[2] for m in my),
                     round(a["net_sum"] / a["actifs"], 2) if a["actifs"] else 0,
                     a["pos"], a["neg"], churn,
                     sum(v for k, v in wed_m.items() if k[:4] == y),
                     anciens_y.get(y, 0), og,
                     round(100.0 * og / a["actifs"], 1) if a["actifs"] else "",
                     a["listers"], rev_y if rev_y else ""])
        prev_y_act = a["actifs"]
    with open(f"{outdir}/pulse.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(PULSE_HEADER)
        w.writerows(rows)
    n_months = len(months)
    print(f"pulse : {len(rows)} lignes ({n_months} mois) en {time.time()-t:.0f}s")
    if n_months < 50:
        fail(f"pulse {n_months} mois < 50 — archive incomplete ?")


def step_profiles(con, outdir):
    """profiles_full.csv.gz — holders courants, colonnes prod (_save_profiles)
    sans pseudo (joint cote preda). airdropOnly = 🎯 comme la prod."""
    t = time.time()
    con.execute(f"""CREATE OR REPLACE TABLE profiles_out AS
        SELECT a.wallet, a.holdings, a.distinct_u AS distinct_collectibles,
          a.mints + a.buys AS acquired, a.sells AS sold,
          CASE WHEN a.mints + a.buys > 0
               THEN round(a.holdings::DOUBLE / (a.mints + a.buys), 3) END AS retention,
          round(a.median_hold, 1) AS median_hold_days,
          a.sc AS collectorScore, a.ac AS activityStatus, a.en AS engagementLevel,
          a.value_store, a.value_floor, a.qb AS qty_bucket,
          CASE WHEN a.mints > 0 AND a.buys = 0 AND a.sells = 0
                AND coalesce(aw.air_mints, 0) >= a.mints THEN '🎯' ELSE '' END AS airdropOnly,
          coalesce(strftime(a.last_day, '%Y-%m-%d'), '') AS last_active,
          a.listed
        FROM attrs a LEFT JOIN air_wallets aw USING (wallet)
        WHERE a.holdings > 0""")
    con.execute(f"""COPY (SELECT * FROM profiles_out ORDER BY wallet)
        TO '{outdir}/profiles_full.csv.gz' (HEADER, COMPRESSION gzip)""")
    print(f"profiles : {time.time()-t:.0f}s —",
          con.execute("SELECT count(*) FROM profiles_out").fetchone())
    for col in ("collectorScore", "activityStatus", "engagementLevel"):
        print(f"  {col} :", con.execute(
            f"SELECT {col}, count(*) FROM profiles_out GROUP BY 1 ORDER BY 2 DESC").fetchall())


def step_reveils(con, outdir):
    """_Reveils : 1re activite dans la fenetre {REVEIL_FENETRE} j apres
    une absence de plus de {REVEIL_GAP} j (patron prod)."""
    con.execute(f"""COPY (
        SELECT dedans AS date_pt, count(*) AS anciens
        FROM engagement
        WHERE dedans IS NOT NULL AND veille IS NOT NULL
          AND (dedans - veille) > {REVEIL_GAP}
        GROUP BY dedans ORDER BY dedans)
        TO '{outdir}/reveils.csv' (HEADER)""")
    print("reveils :", con.execute(
        f"SELECT count(*), coalesce(sum(anciens),0) FROM read_csv('{outdir}/reveils.csv')").fetchone())


def step_size_whales(con, outdir):
    """wallet_size.csv (_size_distribution prod) + whales.csv (3 tops)."""
    rows = []
    q = con.execute(f"""
        SELECT {qty_bucket_sql('holdings')} AS b, count(*), sum(holdings)
        FROM profiles_out WHERE holdings > 0 GROUP BY 1""").fetchall()
    d = {b: (int(w), int(t)) for b, w, t in q}
    tw = sum(v[0] for v in d.values())
    tt = sum(v[1] for v in d.values())
    for b in QTY_ORDER:
        w_, t_ = d.get(b, (0, 0))
        rows.append(["quantity", b, w_,
                     round(100.0 * w_ / tw, 2) if tw else 0, t_,
                     round(100.0 * t_ / tt, 2) if tt else 0])
    for dim, col in (("value_store", "value_store"), ("value_floor", "value_floor")):
        q = con.execute(f"""
            SELECT {value_bucket_sql(col)} AS b, count(*), sum({col})
            FROM profiles_out WHERE {col} > 0 GROUP BY 1""").fetchall()
        d = {b: (int(w), float(t)) for b, w, t in q}
        tw = sum(v[0] for v in d.values())
        tv = sum(v[1] for v in d.values())
        for b in VALUE_ORDER:
            w_, v_ = d.get(b, (0, 0.0))
            rows.append([dim, b, w_,
                         round(100.0 * w_ / tw, 2) if tw else 0, round(v_, 2),
                         round(100.0 * v_ / tv, 2) if tv else 0])
    with open(f"{outdir}/wallet_size.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["dimension", "bucket", "wallets", "pct_wallets", "total", "pct_total"])
        w.writerows(rows)
    wrows = []
    for title, key in WHALE_TYPES:
        top = con.execute(f"""
            SELECT wallet, {key}, holdings, distinct_collectibles, value_store,
                   value_floor, collectorScore, activityStatus
            FROM profiles_out ORDER BY {key} DESC, wallet LIMIT {WHALE_TOP}""").fetchall()
        for rank, (w_, metric, h, dc, vs, vf, sc, ac) in enumerate(top, 1):
            wrows.append([title, rank, w_, metric, h, dc, vs, vf, sc, ac])
    with open(f"{outdir}/whales.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["block", "rank", "wallet", "metric", "holdings",
                    "distinct", "value_store", "value_floor",
                    "collectorScore", "activityStatus"])
        w.writerows(wrows)
    print(f"wallet_size : {len(rows)} lignes | whales : {len(wrows)} lignes")


def step_meta(con, outdir):
    """Garde-fous croisés + meta_ledger.csv."""
    n_circ, n_hold = con.execute(
        "SELECT sum(c), count(DISTINCT wallet) FROM hc").fetchone()
    s_prof = con.execute("SELECT sum(holdings) FROM profiles_out").fetchone()[0]
    n_prof = con.execute("SELECT count(*) FROM profiles_out").fetchone()[0]
    if s_prof != n_circ:
        fail(f"somme holdings profils {s_prof} != circulant {n_circ}")
    air_n = con.execute("SELECT count(*) FROM airdrops").fetchone()[0]
    with open(f"{outdir}/meta_ledger.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["run_date", "circulant", "holders", "profils", "airdrops_detectes"])
        w.writerow([RUN_DATE, int(n_circ), int(n_hold), int(n_prof), air_n])
    print(f"meta : circulant {n_circ:,} | holders {n_hold:,} | profils {n_prof:,}")


def main():
    pq = sys.argv[1] if len(sys.argv) > 1 else "transfers.parquet"
    cat_path = sys.argv[2] if len(sys.argv) > 2 else "catalogue.csv.gz"
    outdir = sys.argv[3] if len(sys.argv) > 3 else "derived"
    os.makedirs(outdir, exist_ok=True)
    steps = (E("STEPS", "all")).split(",")
    con = duckdb.connect(E("DB_PATH", "ledger_work.duckdb"))
    con.execute(f"SET memory_limit='{E('DUCK_MEM', '10GB')}'")
    con.execute("SET temp_directory='duck_spill'")
    if os.environ.get("DUCK_THREADS"):
        con.execute(f"SET threads={int(os.environ['DUCK_THREADS'])}")
    if os.environ.get("DUCK_TMP_MAX"):
        con.execute(f"PRAGMA max_temp_directory_size='{os.environ['DUCK_TMP_MAX']}'")
    con.execute("SET preserve_insertion_order=false")
    t0 = time.time()
    run = lambda s: "all" in steps or s in steps
    if run("views") or True:
        step_views(con, pq)
    if run("catalogue"):
        step_catalogue(con, cat_path)
    if run("ledger"):
        step_ledger(con, outdir)
    if run("segments"):
        step_segments(con)
    if run("engagement"):
        step_engagement(con)
    if run("attrs"):
        step_attrs(con)
    if run("pulse_tables"):
        step_pulse_tables(con)
    if run("corner"):
        step_corner(con, outdir)
    if run("pulse"):
        step_pulse_csv(con, outdir)
    if run("profiles"):
        step_profiles(con, outdir)
    if run("reveils"):
        step_reveils(con, outdir)
    if run("size_whales"):
        step_size_whales(con, outdir)
    if run("meta"):
        step_meta(con, outdir)
    print(f"TOTAL ledger_derived : {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
