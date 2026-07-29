"""
data/db.py — FlowsDB connections.

Two engines, deliberately separate:

    get_engine()        read path — prices_daily and everything else the
                        strategy layer consumes. Used by data.loader.
    get_write_engine()  write path — the systematic.* schema published by
                        data.publish. Falls back to the read credentials when
                        DB_WRITE_USER is unset, so a single-role setup still
                        works; point it at a dedicated role in production so
                        the serving path physically cannot write.
"""

import os
from dotenv import load_dotenv
import pandas as pd
from sqlalchemy import create_engine, text

load_dotenv()

_engine = None
_write_engine = None


def _url(user: str, password: str) -> str:
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "Flows")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def get_engine():
    global _engine
    if _engine is None:
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "")
        _engine = create_engine(_url(user, password), pool_pre_ping=True)
    return _engine


def get_write_engine():
    """Engine for the systematic.* publish schema (see module docstring)."""
    global _write_engine
    if _write_engine is None:
        user = os.getenv("DB_WRITE_USER") or os.getenv("DB_USER", "postgres")
        password = (os.getenv("DB_WRITE_PASSWORD")
                    if os.getenv("DB_WRITE_USER")
                    else os.getenv("DB_PASSWORD", ""))
        _write_engine = create_engine(_url(user, password or ""), pool_pre_ping=True)
    return _write_engine


def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


# ── write helpers ────────────────────────────────────────────────────────────

def execute(sql: str, params: dict | None = None):
    """Run a single statement on the write engine and commit."""
    with get_write_engine().begin() as conn:
        return conn.execute(text(sql), params or {})


def execute_script(sql: str) -> None:
    """
    Run a multi-statement SQL script (the schema DDL) on the write engine.

    Uses exec_driver_sql so psycopg2 sees the script verbatim — SQLAlchemy's
    text() would try to parse ':' inside the DDL as bind parameters.
    """
    with get_write_engine().begin() as conn:
        conn.exec_driver_sql(sql)


def upsert(table: str,
           rows: list[dict],
           conflict_cols: list[str],
           jsonb_cols: tuple[str, ...] = (),
           touch_col: str | None = "updated_at",
           pre_delete: tuple[str, dict] | None = None) -> int:
    """
    Batch INSERT ... ON CONFLICT DO UPDATE. Returns the number of rows sent.

    Every row must carry the same keys — the column list is taken from rows[0],
    so a caller building rows with conditional keys would silently write a
    ragged batch. Callers here always emit a fixed shape.

    Columns named in `jsonb_cols` are passed as JSON strings and cast in SQL;
    psycopg2 has no native adapter for dict -> jsonb.

    `touch_col` is stamped with now() on update (pass None for tables without
    an updated_at column).

    `pre_delete` is a (where_clause, params) pair executed against `table` in
    the SAME transaction before the insert. Snapshot tables need it: an upsert
    alone never removes rows whose natural key changed or left the universe,
    so a republish would leave stale rows interleaved with fresh ones.
    """
    if not rows:
        return 0

    cols = list(rows[0].keys())
    placeholders = ", ".join(
        f"CAST(:{c} AS jsonb)" if c in jsonb_cols else f":{c}" for c in cols
    )
    update_cols = [c for c in cols if c not in conflict_cols]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    if update_cols and touch_col:
        set_clause += f", {touch_col} = now()"

    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_cols)}) "
        + (f"DO UPDATE SET {set_clause}" if update_cols else "DO NOTHING")
    )

    with get_write_engine().begin() as conn:
        if pre_delete is not None:
            where, del_params = pre_delete
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), del_params)
        conn.execute(text(sql), rows)
    return len(rows)
