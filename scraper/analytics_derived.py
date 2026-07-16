#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analytics_derived — couche analyse DuckDB sur transfers-full (jetonveve).

Lit transfers_full.csv.gz (Release `transfers-full`) et produit des CSV
compacts pour la Release `analytics-derived` (lus ensuite cote preda pour
ecrire le Sheet — jointure des noms via le catalogue, ici uuid seulement) :

  meta.csv               run_date, lignes, editions, holders, controles
  ledger_statuts.csv     editions par statut (held/listed/burned/stock)
  wallets_par_profil.csv wallets par profil d'activite (Actif..Fantome)
  supply_par_profil.csv  supply held+listed par profil du detenteur
  corner_items.csv       par item : circulating, holders, burned, stock,
                         ghost_supply/wallets, pct_ghost, gini, top1, top10
  wallet_profils.csv.gz  wallet, last_active, profil (~1,2 M lignes)

Methode validee en proto le 16/07/2026 (voir PROTO-DUCKDB-RESULTATS.md,
repo prive) : dernier etat par (uuid, edition) via max(seq) + jointure sur
seq unique ; last_active par wallet avec exclusion du re-mint de migration
(28/01/2026, source systeme), comme le fix ledger prod du 15/07.

Usage : python -m scraper.analytics_derived <transfers_full.csv[.gz]> [outdir]
Env   : RUN_DATE=YYYY-MM-DD (defaut : aujourd'hui UTC — le workflow passe la
        date PT), DUCK_MEM (defaut 10GB), EXPECTED_MIN (defaut 35000000),
        PARQUET_OUT=chemin (publie le parquet au lieu de le jeter — consomme
        ensuite par URL via DuckDB httpfs, lecture partielle sans telecharger),
        KEEP_PARQUET=1 (debug).
"""
import os
import sys
import time

import duckdb

# ── constantes on-chain ──────────────────────────────────────────────────────
ZERO = "0x0000000000000000000000000000000000000000"
ESCROW = "0xb1af72a77b9065c55cda0680b86655a79b62e42c"
BURN_SINK = "0x39e3816a8c549ec22cd1a34a8cf7034b3941d8b1"
DISTRIB = ("0x7be178ba43a9828c22997a3ec3640497d88d2fd3",   # VeveCollection (hub)
           "0xdb721de5f825fcb3d2dbe3a4778e34e43ae7c095",   # admin transferts
           "0xc4817870a6a75704985be4f9933643a27739afc1")   # VeveStore
SYS_LIST = "('" + "','".join((ZERO, ESCROW, BURN_SINK) + DISTRIB) + "')"
DISTRIB_LIST = "('" + "','".join(DISTRIB) + "')"
MIGRATION_DAY = "2026-01-28"          # re-mint de masse IMX -> CollectChain

COLS = ("{'era':'VARCHAR','ts_utc':'TIMESTAMP','date_pt':'DATE','kind':'VARCHAR',"
        "'category':'VARCHAR','veve_uuid':'VARCHAR','edition':'INTEGER',"
        "'from':'VARCHAR','to':'VARCHAR','tx_ref':'VARCHAR'}")

BURNED = f"(dst IS NULL OR dst='' OR dst='{ZERO}' OR dst='{BURN_SINK}' OR kind='burn')"
LISTED = f"(kind='listing' OR dst='{ESCROW}')"


def fail(msg: str) -> None:
    print(f"ERREUR GARDE-FOU : {msg}")
    sys.exit(1)


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "transfers_full.csv.gz"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "derived"
    run_date = os.environ.get("RUN_DATE") or time.strftime("%Y-%m-%d")
    expected_min = int(os.environ.get("EXPECTED_MIN") or 35_000_000)
    os.makedirs(outdir, exist_ok=True)
    pq = "transfers_tmp.parquet"      # hors outdir : jamais uploade tel quel

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCK_MEM', '10GB')}'")
    con.execute("SET temp_directory='duck_spill'")

    # ── 1. parquet avec seq = ordre du fichier (= ordre chronologique) ──────
    t0 = time.time()
    con.execute("SET preserve_insertion_order=true")
    con.execute(f"""
        COPY (
          SELECT era, ts_utc, date_pt, kind, category,
                 try_cast(nullif(veve_uuid,'') AS UUID) AS uuid, edition,
                 "from" AS frm, "to" AS dst,
                 row_number() OVER () AS seq
          FROM read_csv('{src}', header=true, columns={COLS})
        ) TO '{pq}' (FORMAT parquet, COMPRESSION zstd)""")
    con.execute("SET preserve_insertion_order=false")
    n_rows = con.execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()[0]
    print(f"parquet : {n_rows:,} lignes en {time.time() - t0:.0f}s")
    if n_rows < expected_min:
        fail(f"{n_rows} lignes < EXPECTED_MIN {expected_min} — archive incomplete ?")

    # ── 2. grand livre : max(seq) par edition puis jointure sur seq unique ──
    t1 = time.time()
    con.execute(f"""
        CREATE TABLE last_seq AS
        SELECT max(seq) AS seq FROM read_parquet('{pq}')
        WHERE uuid IS NOT NULL AND edition IS NOT NULL
        GROUP BY uuid, edition""")
    con.execute(f"""
        CREATE TABLE ledger AS
        SELECT p.uuid, p.edition, p.category,
          CASE WHEN {BURNED} THEN 'burned'
               WHEN coalesce(CASE WHEN {LISTED} THEN p.frm ELSE p.dst END,'')
                    IN {DISTRIB_LIST} THEN 'stock'
               WHEN {LISTED} THEN 'listed' ELSE 'held' END AS status,
          CASE WHEN {BURNED} THEN NULL
               WHEN {LISTED} THEN p.frm ELSE p.dst END AS holder
        FROM read_parquet('{pq}') p JOIN last_seq USING(seq)""")
    print(f"grand livre : {time.time() - t1:.0f}s")

    # ── 3. activite wallet (re-mint migration exclu) + profil ───────────────
    t2 = time.time()
    con.execute(f"""
        CREATE TABLE wallet_prof AS
        SELECT wallet, last_active,
          CASE WHEN d<=7 THEN 'Actif'      WHEN d<=30 THEN 'Engage'
               WHEN d<=90 THEN 'Somnolant' WHEN d<=180 THEN 'Inactif'
               WHEN d<=365 THEN 'Desinscrit' ELSE 'Fantome' END AS profil
        FROM (
          SELECT wallet, max(ts_utc) AS last_active,
                 date_diff('day', max(ts_utc)::DATE, DATE '{run_date}') AS d
          FROM (
            SELECT frm AS wallet, ts_utc FROM read_parquet('{pq}')
              WHERE frm IS NOT NULL AND frm<>'' AND frm NOT IN {SYS_LIST}
            UNION ALL
            SELECT dst, ts_utc FROM read_parquet('{pq}')
              WHERE dst IS NOT NULL AND dst<>'' AND dst NOT IN {SYS_LIST}
                AND NOT (date_pt = DATE '{MIGRATION_DAY}'
                         AND (frm IS NULL OR frm='' OR frm IN {SYS_LIST}))
          ) GROUP BY wallet)""")
    print(f"profils wallets : {time.time() - t2:.0f}s")

    # ── 4. corner par item + concentration ──────────────────────────────────
    t3 = time.time()
    con.execute("""
        CREATE TABLE corner AS
        SELECT l.uuid, any_value(l.category) AS category,
          count(*) FILTER (l.status IN ('held','listed'))          AS circulating,
          count(DISTINCT l.holder) FILTER (l.status IN ('held','listed')) AS holders,
          count(*) FILTER (l.status='burned')                      AS burned,
          count(*) FILTER (l.status='stock')                       AS stock,
          count(*) FILTER (l.status IN ('held','listed') AND w.profil='Fantome')
                                                                   AS ghost_supply,
          count(DISTINCT l.holder) FILTER (l.status IN ('held','listed')
                                           AND w.profil='Fantome') AS ghost_wallets
        FROM ledger l LEFT JOIN wallet_prof w ON l.holder = w.wallet
        GROUP BY l.uuid""")
    con.execute("""
        CREATE TABLE concentration AS
        WITH h AS (SELECT uuid, holder, count(*) AS qty FROM ledger
                   WHERE status IN ('held','listed') GROUP BY 1, 2),
        r AS (SELECT uuid, qty,
                row_number() OVER (PARTITION BY uuid ORDER BY qty)      AS rn,
                row_number() OVER (PARTITION BY uuid ORDER BY qty DESC) AS rd,
                count(*)  OVER (PARTITION BY uuid) AS n,
                sum(qty)  OVER (PARTITION BY uuid) AS tot
              FROM h)
        SELECT uuid,
          round(sum((2.0*rn - n - 1)*qty)/(any_value(n)*any_value(tot)), 4) AS gini,
          sum(qty) FILTER (rd=1)   AS top1,
          sum(qty) FILTER (rd<=10) AS top10
        FROM r GROUP BY uuid""")
    print(f"corner + gini : {time.time() - t3:.0f}s")

    # ── 5. controles de coherence ────────────────────────────────────────────
    n_edit = con.execute("SELECT count(*) FROM ledger").fetchone()[0]
    n_circ = con.execute(
        "SELECT count(*) FROM ledger WHERE status IN ('held','listed')").fetchone()[0]
    n_hold = con.execute("SELECT count(DISTINCT holder) FROM ledger "
                         "WHERE status IN ('held','listed')").fetchone()[0]
    s_prof = con.execute("""SELECT count(*) FROM ledger l
        LEFT JOIN wallet_prof w ON l.holder=w.wallet
        WHERE l.status IN ('held','listed')""").fetchone()[0]
    s_corner = con.execute("SELECT sum(circulating) FROM corner").fetchone()[0]
    if s_prof != n_circ:
        fail(f"supply par profil {s_prof} != circulant {n_circ}")
    if s_corner != n_circ:
        fail(f"somme corner {s_corner} != circulant {n_circ}")
    ghost = con.execute("""SELECT count(*) FROM ledger l
        JOIN wallet_prof w ON l.holder=w.wallet
        WHERE l.status IN ('held','listed') AND w.profil='Fantome'""").fetchone()[0]
    print(f"editions {n_edit:,} | circulant {n_circ:,} | holders {n_hold:,} | "
          f"supply fantome {ghost:,} ({100.0*ghost/n_circ:.1f} %)")

    # ── 6. exports ───────────────────────────────────────────────────────────
    con.execute(f"""COPY (SELECT '{run_date}' AS run_date, {n_rows} AS lignes,
        {n_edit} AS editions, {n_circ} AS circulant, {n_hold} AS holders,
        {ghost} AS ghost_supply) TO '{outdir}/meta.csv' (HEADER)""")
    con.execute(f"""COPY (SELECT status, count(*) AS editions FROM ledger
        GROUP BY 1 ORDER BY 2 DESC) TO '{outdir}/ledger_statuts.csv' (HEADER)""")
    con.execute(f"""COPY (SELECT profil, count(*) AS wallets FROM wallet_prof
        GROUP BY 1 ORDER BY 2 DESC) TO '{outdir}/wallets_par_profil.csv' (HEADER)""")
    con.execute(f"""COPY (SELECT coalesce(w.profil,'Non classe') AS profil,
          count(*) AS supply, count(DISTINCT l.holder) AS wallets,
          round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct
        FROM ledger l LEFT JOIN wallet_prof w ON l.holder=w.wallet
        WHERE l.status IN ('held','listed')
        GROUP BY 1 ORDER BY 2 DESC) TO '{outdir}/supply_par_profil.csv' (HEADER)""")
    con.execute(f"""COPY (SELECT k.*,
          round(100.0*k.ghost_supply/nullif(k.circulating,0),1) AS pct_ghost,
          g.gini, g.top1, g.top10
        FROM corner k LEFT JOIN concentration g USING(uuid)
        ORDER BY k.ghost_supply DESC) TO '{outdir}/corner_items.csv' (HEADER)""")
    con.execute(f"""COPY (SELECT wallet, last_active, profil FROM wallet_prof
        ORDER BY wallet)
        TO '{outdir}/wallet_profils.csv.gz' (HEADER, COMPRESSION gzip)""")

    print("--- apercu top 5 supply fantome (uuid, circ, ghost) :")
    for row in con.execute("""SELECT uuid, circulating, ghost_supply FROM corner
            ORDER BY ghost_supply DESC LIMIT 5""").fetchall():
        print("  ", row)
    pq_out = os.environ.get("PARQUET_OUT")
    if pq_out:
        os.replace(pq, pq_out)
        print(f"parquet publie : {pq_out} ({os.path.getsize(pq_out)/1e6:.0f} Mo)")
    elif not os.environ.get("KEEP_PARQUET") and os.path.exists(pq):
        os.remove(pq)
    print(f"TOTAL : {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
