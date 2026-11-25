"""List every table in one or more Postgres databases with its row count.

Usage:
    python table_counts.py                 # databases from DB_NAMES in .env
    python table_counts.py db_a db_b       # these databases instead
"""
import os
import sys

import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

load_dotenv()

TABLES_QUERY = """
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_type = 'BASE TABLE'
      AND table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name
"""


def count_tables(dbname):
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        dbname=dbname,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(TABLES_QUERY)
            tables = cur.fetchall()
            rows = []
            for schema, table in tables:
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
                rows.append((f"{schema}.{table}", cur.fetchone()[0]))
        return rows
    finally:
        conn.close()


def main():
    dbnames = sys.argv[1:] or [
        d.strip() for d in os.getenv("DB_NAMES", "").split(",") if d.strip()
    ]
    if not dbnames:
        sys.exit("No databases given. Set DB_NAMES in .env or pass names as arguments.")

    for dbname in dbnames:
        print(f"\n=== {dbname} ===")
        try:
            rows = count_tables(dbname)
        except psycopg2.Error as e:
            print(f"  ERROR: {e}".rstrip())
            continue
        if not rows:
            print("  (no tables)")
            continue
        width = max(len(name) for name, _ in rows)
        for name, count in rows:
            print(f"  {name:<{width}}  {count:>12,}")
        print(f"  {len(rows)} tables, {sum(c for _, c in rows):,} rows total")


if __name__ == "__main__":
    main()
