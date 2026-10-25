"""SQLite tools: list tables, describe a table, run a read-only SELECT.

Two layers of safety:
  1. The connection is opened read-only (`mode=ro`), so SQLite itself refuses writes.
  2. Only a single SELECT / WITH statement is accepted, with a row limit and a timeout.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .tools import Tool

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
QUERY_TIMEOUT_SECONDS = 5.0

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class QueryError(Exception):
    pass


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise QueryError(f"Database not found: {db_path}. Run scripts/seed_db.py first.")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def validate_select(sql: str) -> str:
    """Return the cleaned statement, or raise QueryError explaining what is allowed."""
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryError("Empty query.")
    if ";" in statement:
        raise QueryError("Only one statement is allowed (remove extra ';').")
    first_word = statement.split(None, 1)[0].lower()
    if first_word not in ("select", "with"):
        raise QueryError("Only read-only SELECT (or WITH ... SELECT) queries are allowed.")
    return statement


def format_rows(columns: list[str], rows: list[tuple]) -> str:
    """Render rows as a compact pipe-separated table that models read easily."""
    lines = [" | ".join(columns)]
    lines += [" | ".join("NULL" if v is None else str(v) for v in row) for row in rows]
    return "\n".join(lines)


def _run_query(db_path: Path, sql: str, limit: int) -> str:
    statement = validate_select(sql)
    limit = max(1, min(int(limit), MAX_LIMIT))
    conn = connect_readonly(db_path)
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    # Called every N SQLite VM steps; returning non-zero aborts the query.
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cursor = conn.execute(statement)
        rows = cursor.fetchmany(limit + 1)
        columns = [d[0] for d in cursor.description or []]
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc):
            raise QueryError(f"Query took longer than {QUERY_TIMEOUT_SECONDS}s and was stopped.")
        raise QueryError(f"SQLite error: {exc}")
    except sqlite3.Error as exc:
        raise QueryError(f"SQLite error: {exc}")
    finally:
        conn.close()

    more = len(rows) > limit
    rows = rows[:limit]
    text = format_rows(columns, rows)
    footer = f"\n({len(rows)} rows" + (f", more rows exist; raise limit or filter)" if more else ")")
    return text + footer


def _list_tables(db_path: Path) -> str:
    conn = connect_readonly(db_path)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        lines = []
        for name in names:
            count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            lines.append(f"{name} ({count} rows)")
    finally:
        conn.close()
    return "\n".join(lines) or "(no tables)"


def _describe_table(db_path: Path, table: str) -> str:
    if not _IDENTIFIER.match(table or ""):
        raise QueryError(f"Invalid table name: {table!r}")
    conn = connect_readonly(db_path)
    try:
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not cols:
            raise QueryError(f"Table '{table}' does not exist. Use db_list_tables.")
        schema = "\n".join(f"- {c[1]} {c[2]}{' PRIMARY KEY' if c[5] else ''}" for c in cols)
        cursor = conn.execute(f'SELECT * FROM "{table}" LIMIT 3')
        sample = format_rows([d[0] for d in cursor.description], cursor.fetchall())
    finally:
        conn.close()
    return f"Columns of {table}:\n{schema}\n\nSample rows:\n{sample}"


def make_db_tools(db_path: Path) -> list[Tool]:
    """Create the three DB tools bound to one database file."""

    async def list_tables(args: dict[str, Any]) -> str:
        return await asyncio.to_thread(_list_tables, db_path)

    async def describe_table(args: dict[str, Any]) -> str:
        return await asyncio.to_thread(_describe_table, db_path, args.get("table", ""))

    async def query(args: dict[str, Any]) -> str:
        return await asyncio.to_thread(
            _run_query, db_path, args.get("sql", ""), args.get("limit", DEFAULT_LIMIT)
        )

    return [
        Tool(
            name="db_list_tables",
            description="List all tables in the SQLite database with their row counts. Start here.",
            parameters={"type": "object", "properties": {}},
            handler=list_tables,
        ),
        Tool(
            name="db_describe_table",
            description="Show the columns and 3 sample rows of a table. Use before writing a query.",
            parameters={
                "type": "object",
                "properties": {"table": {"type": "string", "description": "Table name"}},
                "required": ["table"],
            },
            handler=describe_table,
        ),
        Tool(
            name="db_query",
            description=(
                "Run ONE read-only SQLite SELECT (or WITH ... SELECT) query and return the rows. "
                "The database cannot be modified. Use SQLite date functions, e.g. date('now','-30 day')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement"},
                    "limit": {
                        "type": "integer",
                        "description": f"Max rows to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT})",
                    },
                },
                "required": ["sql"],
            },
            handler=query,
        ),
    ]
