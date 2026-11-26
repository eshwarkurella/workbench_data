"""Write the schema of the workbench database(s) to a readable Markdown file, one per database.

For every table: row count, columns (type, nullable, default), primary key, foreign keys (both ways),
the keys found inside JSON columns, and a few sample rows. At the top: every column / JSON key whose name
matches a search word (vendor, parent, child, source, ...), to find how defects link to each other.

Usage:
    python schema_dump.py                     # databases in DB_NAMES  -> schema/<db>.md
    python schema_dump.py db_a                # one database
    python schema_dump.py --samples 0         # no sample rows
    python schema_dump.py --find vendor,parent,child,source,external,ref
"""

import argparse
import json
import os
import re
from datetime import datetime

from psycopg2 import sql

from table_counts import connect, db_names, list_tables

DEFAULT_FIND = "vendor,parent,child,source,external,ref,original,link,master"
JSON_SAMPLE = 5000          # rows read per JSON column to collect its keys
MAX_CELL = 120              # characters shown per value in the sample rows


def q(cur, query, *params):
    cur.execute(query, params)
    return cur.fetchall()


def columns(cur, schema, table):
    return q(cur, """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum)
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum""", f'"{schema}"."{table}"')


def constraints(cur, schema, table):
    """(primary key columns, [(name, definition)] of FKs going out, of FKs coming in, unique/check)."""
    rel = f'"{schema}"."{table}"'
    pk = q(cur, """
        SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary""", rel)
    out = q(cur, "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                 "WHERE conrelid = %s::regclass AND contype = 'f' ORDER BY conname", rel)
    inc = q(cur, "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                 "WHERE confrelid = %s::regclass AND contype = 'f' ORDER BY 1, 2", rel)
    other = q(cur, "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                   "WHERE conrelid = %s::regclass AND contype IN ('u', 'c') ORDER BY conname", rel)
    return [r[0] for r in pk], out, inc, other


def json_keys(cur, schema, table, col):
    """Top-level keys of a JSON column (first JSON_SAMPLE rows): [(key, rows having it, example value)]."""
    t, c = sql.Identifier(schema, table), sql.Identifier(col)
    cur.execute(sql.SQL("""
        WITH s AS (SELECT {c}::jsonb AS j FROM {t} WHERE {c} IS NOT NULL LIMIT %s)
        SELECT k, count(*), min(s.j ->> k)
        FROM s, jsonb_object_keys(CASE WHEN jsonb_typeof(s.j) = 'object' THEN s.j ELSE '{{}}'::jsonb END) k
        GROUP BY k ORDER BY count(*) DESC, k""").format(c=c, t=t), (JSON_SAMPLE,))
    return cur.fetchall()


def samples(cur, schema, table, n):
    cur.execute(sql.SQL("SELECT * FROM {} LIMIT %s").format(sql.Identifier(schema, table)), (n,))
    return [d[0] for d in cur.description], cur.fetchall()


def cell(v):
    if v is None:
        return ""
    s = json.dumps(v, default=str, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "…"


def dump_db(dbname, n_samples, find):
    conn = connect(dbname)
    conn.set_session(readonly=True)
    pattern = re.compile("|".join(map(re.escape, find)), re.I) if find else None
    body, hits, overview = [], [], []
    try:
        with conn.cursor() as cur:
            for schema, table in list_tables(conn):
                name = f"{schema}.{table}"
                try:
                    count = q(cur, sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table)))[0][0]
                    cols = columns(cur, schema, table)
                    pk, fk_out, fk_in, other = constraints(cur, schema, table)
                    overview.append((name, count, len(cols), ", ".join(pk)))
                    body += [f"\n## {name}  ({count:,} rows)\n"]
                    body += [f"Primary key: {', '.join(pk) or 'none'}\n"]
                    if fk_out:
                        body += ["Links to (foreign keys):"] + [f"- `{d}`  ({n})" for n, d in fk_out] + [""]
                    if fk_in:
                        body += ["Linked from:"] + [f"- {t2}: `{d}`  ({n})" for t2, n, d in fk_in] + [""]
                    if other:
                        body += ["Unique / check:"] + [f"- `{d}`  ({n})" for n, d in other] + [""]
                    body += ["| Column | Type | Null | Default | Comment |", "|---|---|---|---|---|"]
                    for c, typ, nullable, default, comment in cols:
                        body.append(f"| {c} | {typ} | {'yes' if nullable else 'no'} | {cell(default)} | {cell(comment)} |")
                        if pattern and pattern.search(c):
                            hits.append((name, c, f"column ({typ})", ""))
                    for c, typ, *_ in cols:
                        if typ in ("json", "jsonb") and count:
                            keys = json_keys(cur, schema, table, c)
                            if keys:
                                body += ["", f"Keys inside JSON column `{c}` (first {JSON_SAMPLE:,} rows):", "",
                                         "| Key | Rows | Example |", "|---|---|---|"]
                                body += [f"| {k} | {n:,} | {cell(ex)} |" for k, n, ex in keys]
                                hits += [(name, f"{c} -> {k}", "json key", cell(ex)) for k, _, ex in keys
                                         if pattern and pattern.search(k)]
                    if n_samples and count:
                        names, rows = samples(cur, schema, table, n_samples)
                        body += ["", f"Sample rows ({len(rows)}):", "",
                                 "| " + " | ".join(names) + " |", "|" + "---|" * len(names)]
                        body += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
                except Exception as e:
                    conn.rollback()
                    body += [f"\n## {name}\n", f"ERROR: {type(e).__name__}: {str(e).splitlines()[0]}"]
    finally:
        conn.close()

    head = [f"# Schema of `{dbname}`", "", f"Written {datetime.now():%Y-%m-%d %H:%M}.", ""]
    if pattern:
        head += [f"## Columns / JSON keys matching: {', '.join(find)}", ""]
        if hits:
            head += ["| Table | Column / JSON key | Kind | Example |", "|---|---|---|---|"]
            head += [f"| {t} | {c} | {kind} | {ex} |" for t, c, kind, ex in hits]
        else:
            head.append("(none)")
        head.append("")
    head += ["## Tables", "", "| Table | Rows | Columns | Primary key |", "|---|---|---|---|"]
    head += [f"| {t} | {n:,} | {c} | {pk} |" for t, n, c, pk in overview]
    return "\n".join(head + body) + "\n", len(overview), len(hits)


def main():
    ap = argparse.ArgumentParser(description="Write each database's schema to schema/<db>.md")
    ap.add_argument("databases", nargs="*", help="Database names (default: DB_NAMES in .env)")
    ap.add_argument("--samples", type=int, default=3, help="Sample rows per table (default 3, 0 = none)")
    ap.add_argument("--find", default=DEFAULT_FIND, help=f"Comma-separated words to look for (default {DEFAULT_FIND})")
    ap.add_argument("--out", default="schema", help="Output folder (default schema/)")
    a = ap.parse_args()

    dbs = db_names(a.databases)
    if not dbs:
        raise SystemExit("No databases given. Set DB_NAMES in .env or pass names as arguments.")
    find = [w.strip() for w in a.find.split(",") if w.strip()]
    os.makedirs(a.out, exist_ok=True)
    for db in dbs:
        try:
            text, tables, hits = dump_db(db, a.samples, find)
        except Exception as e:
            print(f"{db}: ERROR {type(e).__name__}: {str(e).splitlines()[0]}")
            continue
        path = os.path.join(a.out, f"{db}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"{db}: {tables} tables, {hits} matching columns / JSON keys -> {path}")


if __name__ == "__main__":
    main()
