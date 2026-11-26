# workbench_data

Small tools for the workbench Postgres databases.

- `table_counts.py`: every table with its row count.
- `export_to_s3.py`: every table exported to S3 as Parquet and CSV.

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
