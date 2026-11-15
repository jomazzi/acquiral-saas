"""Database access for the ops console. Uses the restricted acquiral_ops
role (OPS_DATABASE_URL): it can read subscription and account metadata
only, with no privileges on any customer accounting, payroll or bank
table, so a bug here cannot expose them."""
import os

import psycopg2
import psycopg2.extras


def enabled():
    return bool(os.environ.get("OPS_DATABASE_URL") and os.environ.get("OPS_HOST"))


def _run(sql, params, mode):
    conn = psycopg2.connect(os.environ["OPS_DATABASE_URL"], connect_timeout=5)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                if mode == "all":
                    return [dict(r) for r in cur.fetchall()]
                if mode == "one":
                    row = cur.fetchone()
                    return dict(row) if row else None
                return cur.rowcount
    finally:
        conn.close()


def rows(sql, params=None):
    return _run(sql, params, "all")


def one(sql, params=None):
    return _run(sql, params, "one")


def execute(sql, params=None):
    return _run(sql, params, "exec")