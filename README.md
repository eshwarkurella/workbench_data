# workbench_data

Small tools for the workbench Postgres databases.

- `table_counts.py`: every table with its row count.
- `export_to_s3.py`: every table exported to S3 as Parquet and CSV.
- `schema_dump.py`: the schema of each database written to `schema/<db>.md`.
- `defect_breakup.py`: workbench defects split into parent and child, with each vendor defect id, saved to S3.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # then fill in the credentials and S3_ROOT
```

## Table counts

```
python table_counts.py              # databases listed in DB_NAMES
python table_counts.py db_a db_b    # or name them on the command line
```

## Schema

```
python schema_dump.py                       # databases in DB_NAMES -> schema/<db>.md
python schema_dump.py --samples 0           # without sample rows
python schema_dump.py --find vendor,parent  # other words to search for
```

Per table: row count, columns (type, nullable, default, comment), primary key, foreign keys both ways,
unique / check rules, the keys found inside JSON columns, and 3 sample rows. At the top: every column or
JSON key whose name contains vendor, parent, child, source, external, ref, original, link or master.
`schema/` is git-ignored: the sample rows hold real data, including names.

## Defect breakup (parent / child / vendor defect id)

```
python defect_breakup.py              # databases in DB_NAMES -> S3
python defect_breakup.py --local out  # to a local folder instead (to test)
```

Saved to S3 like the tables, as `defect_breakup.parquet` / `.csv`:

```
workbench/year=2026/month=11/day=26/<database>/defect_breakup.parquet   (+ .csv)
workbench/latest/<database>/defect_breakup.parquet                      (+ .csv)
```

`export_to_s3.py` writes the same file with every export, so running the export is enough.

One row per vendor defect. How the workbench stores defects:

- `workbench_defects` holds the parents; `workbench_defect_children.parent_id` points to them.
- **grouped** parent (`PAR-…`): made in the workbench from several vendor defects. Each child row has the
  vendor id (`vendor_defect_id`), its `position` and `is_middle`.
- **single** parent (`<vendor defect id>_<survey id>`): one vendor defect. The vendor id is the `defect_id`
  without the `_<survey id>` ending.
- vendor name: `workbench_runs.record -> meta -> vendor` (or `workbench_run_status.status -> vendor`),
  run id `<upc>__W<survey week>`.

Columns: parent_defect_id, parent_kind, child_count, child_position, child_is_middle, child_id,
vendor_defect_id, vendor_id_from, vendor, survey_id, upc, survey week/date, defect type, category, severity,
status, side, road part, lane, chainages, lat/lon, confidence, first detected date, the parent's type and
chainages, image url, reviewer, pushed at. A summary is printed on screen.

## Export to S3

```
python export_to_s3.py                       # databases in DB_NAMES, today's date (UTC)
python export_to_s3.py db_a                  # one database
python export_to_s3.py --date 2026-11-16     # write into another day's folder
python export_to_s3.py --local out           # write to ./out instead of S3 (to test)
```

Files, under `S3_ROOT`:

```
workbench/year=2026/month=11/day=26/<database>/<schema>.<table>.parquet   (+ .csv)
workbench/latest/<database>/<schema>.<table>.parquet                      (+ .csv)
```

The dated folder is written first. `latest/<database>/` is replaced by a copy of it only when every table
of that database exported fine, so `latest/` always holds one complete export. Running twice on the same day
overwrites that day's files.

Each table is read into memory in one go, so a very large table needs a machine with enough RAM.
