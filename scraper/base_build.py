# -*- coding: utf-8 -*-
"""Construit la BASE LOCALE consolidee veve.duckdb (pour le dossier
Archive/base/ de Preda — analyses ad hoc en session, sans workflow).

Tables :
  transfers : CC + IMX unifies — era, ts_utc, date_pt, kind, category,
              veve_uuid, edition, token_id, wallet_from, wallet_to.
              Dedup CC par (block, log_index), IMX par txn_id (+ filtre
              contrat VeVe ; kind deduit des adresses comme ledger.py).
  holders   : snapshot detenteurs ACTUELS (dedup token_id, dernier run gagne).
  ledger    : grand livre (veve_uuid, edition, holder, listed).
  profiles  : profils comportement wallets.

Env : ARCHIVE_DIR (dl), IMX_DIR (dl_imx), SNAPSHOT_DIR (dl_snap),
      LEDGER_GZ (data/ledger.csv.gz), PROFILES_GZ (data/wallet_profiles.csv.gz),
      BASE_OUT (veve.duckdb), DUCK_MEM (5GB).
"""
import glob
import os
import time

import duckdb

ZERO = "0x0000000000000000000000000000000000000000"
ESCROW = "0xb1af72a77b9065c55cda0680b86655a79b62e42c"
COFFRE = "0x39e3816a8c549ec22cd1a34a8cf7034b3941d8b1"
IMX_CT = "0xa7aefead2f25972d80516628417ac46b3f2604af"


def main() -> int:
    t0 = time.time()
    out = os.environ.get("BASE_OUT", "veve.duckdb")
    if os.path.exists(out):
        os.remove(out)
    db = duckdb.connect(out)
    db.execute("SET memory_limit='%s'" % os.environ.get("DUCK_MEM", "5GB"))
    db.execute("SET temp_directory='duck_tmp'")
    db.execute("SET preserve_insertion_order=false")

    def count(t):
        return db.sql("SELECT count(*) FROM " + t).fetchone()[0]

    cc = sorted(glob.glob(os.path.join(os.environ.get("ARCHIVE_DIR", "dl"),
                                       "*.csv.gz")))
    imx = sorted(glob.glob(os.path.join(os.environ.get("IMX_DIR", "dl_imx"),
                                        "*.csv.gz")))
    hol = sorted(glob.glob(os.path.join(os.environ.get("SNAPSHOT_DIR", "dl_snap"),
                                        "*.csv.gz")))
    print("Sources : %d CC, %d IMX, %d holders." % (len(cc), len(imx), len(hol)),
          flush=True)

    db.execute("""
    CREATE TABLE transfers AS
    SELECT 'cc' AS era, CAST(ts_utc AS TIMESTAMP) AS ts_utc, date_pt, kind,
           category, lower(veve_uuid) AS veve_uuid, edition,
           CAST(NULL AS VARCHAR) AS token_id,
           lower("from") AS wallet_from, lower("to") AS wallet_to
    FROM (SELECT DISTINCT ON (block, log_index) *
          FROM read_csv_auto(""" + repr(cc) + """, all_varchar=true))
    """)
    print("transfers CC : {:,}".format(count("transfers")), flush=True)

    db.execute("""
    INSERT INTO transfers
    SELECT 'imx', to_timestamp(CAST(txn_time_ms AS BIGINT)/1000), date_pt,
           CASE WHEN txn_type = 'mint' OR lower("from") = '""" + ZERO + """' THEN 'mint'
                WHEN lower("to") IN ('""" + ZERO + """', '""" + COFFRE + """') THEN 'burn'
                WHEN lower("to") = '""" + ESCROW + """' THEN 'listing'
                ELSE 'market' END,
           NULL, NULL, NULL, token_id, lower("from"), lower("to")
    FROM (SELECT DISTINCT ON (txn_id) *
          FROM read_csv_auto(""" + repr(imx) + """, all_varchar=true)
          WHERE lower(token_address) = '""" + IMX_CT + """')
    """)
    print("transfers TOTAL : {:,}".format(count("transfers")), flush=True)

    db.execute("""
    CREATE TABLE holders AS
    SELECT token_id, category, lower(veve_uuid) AS veve_uuid, edition,
           total_editions, lower(owner) AS owner, name, rarity, series, mint_date
    FROM (SELECT *, row_number() OVER (PARTITION BY token_id
                                       ORDER BY filename DESC) AS rn
          FROM read_csv_auto(""" + repr(hol) + """, all_varchar=true,
                             filename=true)
          WHERE veve_uuid IS NOT NULL AND edition IS NOT NULL
            AND token_id IS NOT NULL)
    WHERE rn = 1
    """)
    print("holders : {:,}".format(count("holders")), flush=True)

    led = os.environ.get("LEDGER_GZ", "data/ledger.csv.gz")
    pro = os.environ.get("PROFILES_GZ", "data/wallet_profiles.csv.gz")
    if os.path.exists(led):
        db.execute("CREATE TABLE ledger AS SELECT * FROM read_csv_auto('"
                   + led + "', all_varchar=true)")
        print("ledger : {:,}".format(count("ledger")), flush=True)
    if os.path.exists(pro):
        db.execute("CREATE TABLE profiles AS SELECT * FROM read_csv_auto('"
                   + pro + "')")
        print("profiles : {:,}".format(count("profiles")), flush=True)

    db.close()
    print("Base : %s (%.2f Go, %ds)" % (out, os.path.getsize(out) / 1e9,
                                        time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
