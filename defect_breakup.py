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

Output: one row per vendor defect -> defect_breakup/<db>.csv, plus a summary on screen.

Usage:
    python defect_breakup.py              # databases in DB_NAMES
    python defect_breakup.py db_a         # one database
"""

import argparse
import csv
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


def main():
    ap = argparse.ArgumentParser(description="Workbench defects by parent / child with vendor defect ids")
    ap.add_argument("databases", nargs="*", help="Database names (default: DB_NAMES in .env)")
    ap.add_argument("--out", default="defect_breakup", help="Output folder (default defect_breakup/)")
    a = ap.parse_args()

    dbs = db_names(a.databases)
    if not dbs:
        raise SystemExit("No databases given. Set DB_NAMES in .env or pass names as arguments.")
    os.makedirs(a.out, exist_ok=True)
    for db in dbs:
        print(f"\n=== {db} ===")
        conn = connect(db)
        try:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute(QUERY)
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {str(e).splitlines()[0]}")
            continue
        finally:
            conn.close()

        path = os.path.join(a.out, f"{db}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)

        i = {c: n for n, c in enumerate(cols)}
        parents = {}
        for r in rows:
            parents.setdefault(r[i["parent_defect_id"]], (r[i["parent_kind"]], r[i["child_count"]]))
        kinds = {}
        for kind, _ in parents.values():
            kinds[kind] = kinds.get(kind, 0) + 1
        sizes = {}
        for kind, n in parents.values():
            if n:
                sizes[n] = sizes.get(n, 0) + 1
        no_vendor = sum(1 for r in rows if r[i["vendor_id_from"]] == "not found")
        no_vendor_name = sum(1 for r in rows if not r[i["vendor"]])

        print(f"  parents: {len(parents):,}")
        for k, n in sorted(kinds.items()):
            print(f"    {k:24s} {n:,}")
        print(f"  rows (one per vendor defect): {len(rows):,}")
        print(f"    vendor id from child      {sum(1 for r in rows if r[i['vendor_id_from']] == 'child vendor_defect_id'):,}")
        print(f"    vendor id from parent id  {sum(1 for r in rows if r[i['vendor_id_from']] == 'parent defect_id'):,}")
        print(f"    vendor id not found       {no_vendor:,}")
        print(f"    vendor name not found     {no_vendor_name:,}")
        if sizes:
            print("  children per grouped parent: " +
                  ", ".join(f"{n} child{'ren' if n > 1 else ''}: {c:,}" for n, c in sorted(sizes.items())))
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
