import pytest

from app.sql_steps import UnsupportedQueryError, _extract_base_tables, _parse_select_statement, parse_to_steps


def _labels(sql: str) -> list[str]:
    return [label for label, _desc, _sql, _hl in parse_to_steps(sql)]


def _highlights_by_label(sql: str) -> dict[str, list[str]]:
    return {label: hl for label, _desc, _sql, hl in parse_to_steps(sql)}


def test_simple_select_has_from_and_select_steps():
    labels = _labels("SELECT * FROM customers")
    assert labels == ["FROM / JOIN", "SELECT"]


def test_where_adds_step():
    labels = _labels("SELECT name FROM customers WHERE prefecture = '東京都'")
    assert labels == ["FROM / JOIN", "WHERE", "SELECT"]


def test_group_by_having_order_by_limit_all_present():
    sql = """
        SELECT customer_id, COUNT(*) AS order_count
        FROM orders
        WHERE status = '完了'
        GROUP BY customer_id
        HAVING COUNT(*) > 1
        ORDER BY order_count DESC
        LIMIT 5
    """
    labels = _labels(sql)
    assert labels == [
        "FROM / JOIN",
        "WHERE",
        "GROUP BY",
        "HAVING",
        "SELECT",
        "ORDER BY",
        "LIMIT / OFFSET",
    ]


def test_distinct_changes_select_label():
    labels = _labels("SELECT DISTINCT prefecture FROM customers")
    assert "SELECT DISTINCT" in labels


def test_join_is_single_from_step():
    sql = """
        SELECT o.order_id, c.name
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
    """
    labels = _labels(sql)
    assert labels == ["FROM / JOIN", "SELECT"]


def test_final_step_sql_round_trips_full_query_semantics():
    sql = "SELECT name FROM customers ORDER BY name LIMIT 3"
    steps = parse_to_steps(sql)
    last_sql = steps[-1][2]
    assert "ORDER BY" in last_sql
    assert "LIMIT" in last_sql


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO customers (name) VALUES ('x')",
        "SELECT 1 FROM a UNION SELECT 2 FROM b",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SELECT 1",
        "not valid sql at all (((",
    ],
)
def test_unsupported_queries_raise(sql: str):
    with pytest.raises(UnsupportedQueryError):
        parse_to_steps(sql)


def _base_tables(sql: str) -> list[tuple[str, str | None]]:
    stmt = _parse_select_statement(sql)
    return _extract_base_tables(stmt.fromClause)


def test_extract_base_tables_single_table_no_alias():
    assert _base_tables("SELECT * FROM customers") == [("customers", None)]


def test_extract_base_tables_join_preserves_order_and_aliases():
    sql = """
        SELECT o.order_id, c.name
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
    """
    assert _base_tables(sql) == [("orders", "o"), ("customers", "c")]


def test_extract_base_tables_self_join_lists_both_aliases():
    sql = """
        SELECT a.order_id
        FROM orders a
        JOIN orders b ON a.order_id = b.order_id
    """
    assert _base_tables(sql) == [("orders", "a"), ("orders", "b")]


def test_extract_base_tables_three_way_join():
    sql = """
        SELECT *
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        JOIN order_items oi ON oi.order_id = o.order_id
    """
    assert _base_tables(sql) == [("orders", "o"), ("customers", "c"), ("order_items", "oi")]


def test_highlight_columns_join_where_group_by_having_order_by():
    sql = """
        SELECT c.prefecture, COUNT(*) AS order_count
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        WHERE o.status = '完了'
        GROUP BY c.prefecture
        HAVING COUNT(*) > 1
        ORDER BY order_count DESC
        LIMIT 5
    """
    highlights = _highlights_by_label(sql)
    assert highlights["FROM / JOIN"] == ["customer_id"]
    assert highlights["WHERE"] == ["status"]
    assert highlights["GROUP BY"] == ["prefecture"]
    assert highlights["HAVING"] == []  # COUNT(*)は列参照を持たない
    assert highlights["SELECT"] == []
    assert highlights["ORDER BY"] == ["order_count"]
    assert highlights["LIMIT / OFFSET"] == []


def test_highlight_columns_empty_for_single_table_no_join():
    highlights = _highlights_by_label("SELECT * FROM customers")
    assert highlights["FROM / JOIN"] == []
