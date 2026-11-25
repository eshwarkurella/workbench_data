# db-table-counts

Lists every table in one or more Postgres databases with its row count.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # then fill in the credentials
```

## Run

```
python table_counts.py              # databases listed in DB_NAMES
python table_counts.py db_a db_b    # or name them on the command line
```
