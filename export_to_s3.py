"""Export every table of the workbench database(s) to S3 as Parquet and CSV.

Layout under S3_ROOT (from .env):
    <S3_ROOT>/workbench/year=2026/month=11/day=26/<database>/<schema>.<table>.parquet  (+ .csv)
    <S3_ROOT>/workbench/latest/<database>/<schema>.<table>.parquet                     (+ .csv)

The dated folder is written first. Only when every table of a database exported fine is that database's
latest/ folder replaced by a copy of it, so latest/ always holds one complete export.

Usage:
    python export_to_s3.py                    # databases from DB_NAMES, today's date
    python export_to_s3.py db_a db_b          # these databases
    python export_to_s3.py --date 2026-11-16  # write into another day's folder
    python export_to_s3.py --local out        # write to a local folder instead of S3 (to test)
"""

import argparse
import json
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv
from psycopg2 import sql

from table_counts import connect, db_names, list_tables

load_dotenv()

FORMATS = [f.strip().lower() for f in os.getenv("EXPORT_FORMATS", "parquet,csv").split(",") if f.strip()]


# ── reading a table ──────────────────────────────────────────────────────────────────────────────────────
def _plain(v):
    """Values Parquet cannot store as they come from Postgres → text."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, (uuid.UUID, Decimal)):
        return str(v)
    return v


def read_table(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(_plain)
    return df


def write_files(df, folder, name):
    """Writes <name>.parquet / .csv into folder, returns the file names."""
    out = []
    if "parquet" in FORMATS:
        try:
            tbl = pa.Table.from_pandas(df, preserve_index=False)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            # a column mixing types (e.g. numbers and text): store the mixed columns as text
            df = df.copy()
            for c in df.columns:
                if df[c].dtype == object:
                    df[c] = df[c].map(lambda v: None if v is None else str(v))
            tbl = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(tbl, os.path.join(folder, f"{name}.parquet"))
        out.append(f"{name}.parquet")
    if "csv" in FORMATS:
        df.to_csv(os.path.join(folder, f"{name}.csv"), index=False)
        out.append(f"{name}.csv")
    return out


# ── where the files go: S3 or a local folder ─────────────────────────────────────────────────────────────
class S3Target:
    def __init__(self, root):
        import boto3
        if not root.startswith("s3://"):
            sys.exit(f"S3_ROOT must start with s3:// (got {root!r})")
        self.bucket, _, prefix = root[len("s3://"):].partition("/")
        self.prefix = prefix.strip("/")
        self.s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)

    def key(self, *parts):
        return "/".join(p.strip("/") for p in (self.prefix, *parts) if p)

    def upload(self, local_file, *parts):
        self.s3.upload_file(local_file, self.bucket, self.key(*parts))

    def replace_folder(self, src_parts, dst_parts):
        """dst folder := exact copy of src folder (server-side copy, old files in dst removed)."""
        src, dst = self.key(*src_parts) + "/", self.key(*dst_parts) + "/"
        pages = self.s3.get_paginator("list_objects_v2")
        old = [o["Key"] for p in pages.paginate(Bucket=self.bucket, Prefix=dst) for o in p.get("Contents", [])]
        for i in range(0, len(old), 1000):
            self.s3.delete_objects(Bucket=self.bucket,
                                   Delete={"Objects": [{"Key": k} for k in old[i:i + 1000]], "Quiet": True})
        for p in pages.paginate(Bucket=self.bucket, Prefix=src):
            for o in p.get("Contents", []):
                self.s3.copy({"Bucket": self.bucket, "Key": o["Key"]}, self.bucket, dst + o["Key"][len(src):])

    def show(self, *parts):
        return f"s3://{self.bucket}/{self.key(*parts)}"


class LocalTarget:
    def __init__(self, root):
        self.root = root

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def upload(self, local_file, *parts):
        dst = self.path(*parts)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(local_file, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())

    def replace_folder(self, src_parts, dst_parts):
        import shutil
        dst = self.path(*dst_parts)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(self.path(*src_parts), dst)

    def show(self, *parts):
        return self.path(*parts)


# ── main ─────────────────────────────────────────────────────────────────────────────────────────────────
def export_db(dbname, target, day_parts):
    """Exports one database; returns (rows of the summary, ok)."""
    summary, ok = [], True
    conn = connect(dbname)
    try:
        conn.set_session(readonly=True)
        tables = list_tables(conn)
        with tempfile.TemporaryDirectory() as tmp:
            for schema, table in tables:
                name = f"{schema}.{table}"
                try:
                    df = read_table(conn, schema, table)
                    for f in write_files(df, tmp, name):
                        target.upload(os.path.join(tmp, f), *day_parts, dbname, f)
                        os.remove(os.path.join(tmp, f))
                    summary.append((name, len(df), "ok"))
                    print(f"  {name:50s} {len(df):>12,} rows")
                except Exception as e:
                    conn.rollback()
                    ok = False
                    summary.append((name, None, f"FAILED: {type(e).__name__}: {e}".splitlines()[0]))
                    print(f"  {name:50s} FAILED: {type(e).__name__}: {str(e).splitlines()[0]}")
    finally:
        conn.close()
    return summary, ok


def main():
    ap = argparse.ArgumentParser(description="Export workbench tables to S3 as Parquet and CSV")
    ap.add_argument("databases", nargs="*", help="Database names (default: DB_NAMES in .env)")
    ap.add_argument("--date", help="Folder date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--local", help="Write into this local folder instead of S3_ROOT (for testing)")
    a = ap.parse_args()

    dbs = db_names(a.databases)
    if not dbs:
        sys.exit("No databases given. Set DB_NAMES in .env or pass names as arguments.")
    d = date.fromisoformat(a.date) if a.date else datetime.now(timezone.utc).date()
    if a.local:
        target = LocalTarget(a.local)
    else:
        root = os.getenv("S3_ROOT", "").strip()
        if not root:
            sys.exit("Set S3_ROOT in .env (e.g. s3://my-bucket/some/prefix) or use --local <folder>.")
        target = S3Target(root)
    day_parts = ("workbench", f"year={d.year}", f"month={d.month:02d}", f"day={d.day:02d}")
    print(f"Formats {', '.join(FORMATS)} | dated folder {target.show(*day_parts)}")

    failed = []
    for db in dbs:
        print(f"\n=== {db} ===")
        try:
            summary, ok = export_db(db, target, day_parts)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {str(e).splitlines()[0]}")
            failed.append(db)
            continue
        if not summary:
            print("  (no tables)")
            continue
        if ok:
            target.replace_folder((*day_parts, db), ("workbench", "latest", db))
            print(f"  latest updated: {target.show('workbench', 'latest', db)}")
        else:
            failed.append(db)
            print(f"  some tables failed -> latest/{db} NOT updated (it keeps the previous export)")

    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()
