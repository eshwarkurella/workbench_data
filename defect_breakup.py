"""Workbench defects broken up into parent and child, with the vendor defect id of each.

How the workbench stores them:
  workbench_defects           one row per parent defect (defect_id)
  workbench_defect_children   the vendor defects grouped under a parent (parent_id -> workbench_defects.defect_id),
                              with vendor_defect_id, position and is_middle
A parent is either
  grouped   PAR-...: made in the workbench by joining several vendor defects; its vendor ids are on its children
  single    <vendor defect id>_<survey id>: one vendor defect; the vendor id is the defect_id without
            the "_<survey id>" ending
The vendor name comes from workbench_runs (run_id = <upc>__W<survey week>).

Output: one row per vendor defect, as defect_breakup.parquet / .csv in S3 (S3_ROOT in .env):
    <S3_ROOT>/workbench/year=…/month=…/day=…/<db>/defect_breakup.parquet  (+ .csv)
    <S3_ROOT>/workbench/latest/<db>/defect_breakup.parquet               (+ .csv)
plus a summary on screen. export_to_s3.py writes the same file with every export.

Usage:
    python defect_breakup.py              # databases in DB_NAMES
    python defect_breakup.py db_a         # one database
    python defect_breakup.py --local out  # write to a local folder instead of S3 (to test)
"""

import argparse
import os

from table_counts import connect, db_names

SURVEY_ID = r"N/[0-9]{5}/[0-9]{5}/[A-Z]{2}_[0-9]{3}_[0-9]{6}"

QUERY = rf"""
WITH kids AS (SELECT parent_id, count(*) AS n FROM workbench_defect_children GROUP BY parent_id),
rows AS (
    SELECT
        p.defect_id                                          AS parent_defect_id,
        CASE WHEN c.child_id IS NOT NULL THEN 'grouped'
             WHEN p.defect_id LIKE 'PAR-%%' THEN 'grouped (no children)'
             ELSE 'single' END                               AS parent_kind,
        coalesce(k.n, 0)                                     AS child_count,
        c.position                                           AS child_position,
        c.is_middle                                          AS child_is_middle,
        c.child_id,
        coalesce(c.vendor_defect_id,
                 CASE WHEN p.defect_id ~ '_{SURVEY_ID}$'
                      THEN regexp_replace(p.defect_id, '_{SURVEY_ID}$', '') END) AS vendor_defect_id,
        CASE WHEN c.child_id IS NOT NULL THEN 'child vendor_defect_id'
             WHEN p.defect_id ~ '_{SURVEY_ID}$' THEN 'parent defect_id'
             ELSE 'not found' END                            AS vendor_id_from,
        coalesce(substring(c.child_id FROM '_({SURVEY_ID})$'),
                 substring(p.defect_id FROM '_({SURVEY_ID})$')) AS survey_id,
        coalesce(c.data, p.data)                             AS d,
        p.data                                               AS pd
    FROM workbench_defects p
    LEFT JOIN workbench_defect_children c ON c.parent_id = p.defect_id
    LEFT JOIN kids k ON k.parent_id = p.defect_id
)
SELECT r.parent_defect_id, r.parent_kind, r.child_count, r.child_position, r.child_is_middle, r.child_id,
       r.vendor_defect_id, r.vendor_id_from,
       coalesce(wr.record -> 'meta' ->> 'vendor', rs.status ->> 'vendor') AS vendor,
       r.survey_id,
       r.d ->> 'upc'           AS upc,
       r.d ->> 'surveyWeek'    AS survey_week,
       r.d ->> 'surveyDate'    AS survey_date,
       r.d ->> 'defectType'    AS defect_type,
       r.d ->> 'category'      AS category,
       r.d ->> 'severity'      AS severity,
       r.d ->> 'status'        AS status,
       r.d ->> 'side'          AS side,
       r.d ->> 'roadPart'      AS road_part,
       r.d ->> 'lane'          AS lane,
       r.d ->> 'startChainage' AS start_chainage,
       r.d ->> 'endChainage'   AS end_chainage,
       r.d ->> 'startLat'      AS start_lat,
       r.d ->> 'startLon'      AS start_lon,
       r.d ->> 'confidencePct' AS confidence_pct,
       r.d ->> 'firstDetectedDate' AS first_detected_date,
       r.pd ->> 'defectType'   AS parent_defect_type,
       r.pd ->> 'startChainage' AS parent_start_chainage,
       r.pd ->> 'endChainage'  AS parent_end_chainage,
       r.d ->> 'imageUrl'      AS image_url,
       r.d ->> 'reviewerId'    AS reviewer,
       r.d ->> 'pushedAt'      AS pushed_at
FROM rows r
LEFT JOIN workbench_runs wr ON wr.run_id = (r.d ->> 'upc') || '__W' || (r.d ->> 'surveyWeek')
LEFT JOIN workbench_run_status rs ON rs.run_id = (r.d ->> 'upc') || '__W' || (r.d ->> 'surveyWeek')
ORDER BY r.parent_defect_id, r.child_position NULLS FIRST, r.child_id
"""


def read_breakup(conn):
    """The breakup as a DataFrame (one row per vendor defect)."""
    import pandas as pd
    with conn.cursor() as cur:
        cur.execute(QUERY)
        return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def print_summary(df):
    parents = df.drop_duplicates("parent_defect_id")
    print(f"  parents: {len(parents):,}")
    for kind, n in parents["parent_kind"].value_counts().sort_index().items():
        print(f"    {kind:24s} {n:,}")
    src = df["vendor_id_from"].value_counts()
    print(f"  rows (one per vendor defect): {len(df):,}")
    print(f"    vendor id from child      {src.get('child vendor_defect_id', 0):,}")
    print(f"    vendor id from parent id  {src.get('parent defect_id', 0):,}")
    print(f"    vendor id not found       {src.get('not found', 0):,}")
    print(f"    vendor name not found     {int(df['vendor'].isna().sum()):,}")
    sizes = parents.loc[parents["child_count"] > 0, "child_count"].value_counts().sort_index()
    if len(sizes):
        print("  children per grouped parent: " +
              ", ".join(f"{n} child{'ren' if n > 1 else ''}: {c:,}" for n, c in sizes.items()))


def main():
    import tempfile
    from export_to_s3 import FORMATS, LATEST, dated_folder, make_target, write_files

    ap = argparse.ArgumentParser(description="Workbench defects by parent / child with vendor defect ids -> S3")
    ap.add_argument("databases", nargs="*", help="Database names (default: DB_NAMES in .env)")
    ap.add_argument("--date", help="Folder date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--local", help="Write into this local folder instead of S3_ROOT (for testing)")
    a = ap.parse_args()

    dbs = db_names(a.databases)
    if not dbs:
        raise SystemExit("No databases given. Set DB_NAMES in .env or pass names as arguments.")
    target = make_target(a.local)
    day_parts = dated_folder(a.date)
    failed = []
    for db in dbs:
        print(f"\n=== {db} ===")
        conn = connect(db)
        try:
            conn.set_session(readonly=True)
            df = read_breakup(conn)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {str(e).splitlines()[0]}")
            failed.append(db)
            continue
        finally:
            conn.close()
        print_summary(df)
        with tempfile.TemporaryDirectory() as tmp:
            for f in write_files(df, tmp, "defect_breakup"):
                for parts in (day_parts, LATEST):
                    target.upload(os.path.join(tmp, f), *parts, db, f)
                    print(f"  -> {target.show(*parts, db, f)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
