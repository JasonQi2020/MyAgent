import sqlite3

import pytest

from agent_demo.db_tools import QueryError, connect_readonly, make_db_tools, validate_select


def tools_by_name(db):
    return {t.name: t for t in make_db_tools(db)}


async def test_list_and_describe(demo_db):
    tools = tools_by_name(demo_db)
    listing = await tools["db_list_tables"].handler({})
    assert "companies (12 rows)" in listing
    described = await tools["db_describe_table"].handler({"table": "trades"})
    assert "symbol TEXT" in described and "Sample rows" in described


async def test_query_with_limit(demo_db):
    result = await tools_by_name(demo_db)["db_query"].handler(
        {"sql": "SELECT symbol, close FROM daily_prices", "limit": 5}
    )
    assert result.startswith("symbol | close")
    assert "(5 rows, more rows exist" in result


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM trades", "SELECT 1; DROP TABLE trades", "UPDATE trades SET qty=0", ""],
)
def test_rejects_non_select(sql):
    with pytest.raises(QueryError):
        validate_select(sql)


def test_connection_is_really_read_only(demo_db):
    # Even if validation were bypassed, SQLite itself refuses writes.
    conn = connect_readonly(demo_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM trades")
    conn.close()


async def test_describe_rejects_injection(demo_db):
    with pytest.raises(QueryError):
        await tools_by_name(demo_db)["db_describe_table"].handler({"table": 'x"; DROP TABLE t;--'})
