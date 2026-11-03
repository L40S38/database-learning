import pytest

from app.sql_steps import (
    UnsupportedQueryError,
    _cte_names,
    _extract_from_sources,
    _parse_select_statement,
    _preview_table_query,
    parse_to_steps,
    preview_source_tables,
)


def _labels(sql: str) -> list[str]:
    return [label for label, _desc, _sql, _hl in parse_to_steps(sql)]


def _highlights_by_label(sql: str) -> dict[str, list[str]]:
    return {label: hl for label, _desc, _sql, hl in parse_to_steps(sql)}


def _sql_by_label(sql: str) -> dict[str, str]:
    return {label: s for label, _desc, s, _hl in parse_to_steps(sql)}


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
        "SELECT 1",
        "not valid sql at all (((",
        "SELECT * FROM customers FOR UPDATE",
        "SELECT * FROM customers FOR SHARE",
        "SELECT name, ROW_NUMBER() OVER w FROM customers WINDOW w AS (ORDER BY name)",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT * FROM t",
        "SELECT 1 FROM a UNION SELECT 2 FROM b UNION SELECT 3 FROM c",
    ],
)
def test_unsupported_queries_raise(sql: str):
    with pytest.raises(UnsupportedQueryError):
        parse_to_steps(sql)


def _from_sources(sql: str) -> list[tuple[str, str | None, str | None]]:
    stmt = _parse_select_statement(sql)
    sources = _extract_from_sources(stmt.fromClause, _cte_names(stmt))
    return [(s.kind, s.relname, s.alias) for s in sources]


def test_extract_from_sources_single_table_no_alias():
    assert _from_sources("SELECT * FROM customers") == [("table", "customers", None)]


def test_extract_from_sources_join_preserves_order_and_aliases():
    sql = """
        SELECT o.order_id, c.name
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
    """
    assert _from_sources(sql) == [("table", "orders", "o"), ("table", "customers", "c")]


def test_extract_from_sources_self_join_lists_both_aliases():
    sql = """
        SELECT a.order_id
        FROM orders a
        JOIN orders b ON a.order_id = b.order_id
    """
    assert _from_sources(sql) == [("table", "orders", "a"), ("table", "orders", "b")]


def test_extract_from_sources_three_way_join():
    sql = """
        SELECT *
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        JOIN order_items oi ON oi.order_id = o.order_id
    """
    assert _from_sources(sql) == [
        ("table", "orders", "o"),
        ("table", "customers", "c"),
        ("table", "order_items", "oi"),
    ]


def test_extract_from_sources_cte_reference_classified_as_cte():
    sql = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    assert _from_sources(sql) == [("cte", "recent", None)]


def test_extract_from_sources_derived_table_classified_as_subquery():
    sql = "SELECT * FROM (SELECT * FROM orders) AS sub"
    assert _from_sources(sql) == [("subquery", None, "sub")]


def test_extract_from_sources_schema_qualified_name_is_not_cte():
    # スキーマ付きの参照は、同名のCTEがあっても実テーブルを指す
    sql = "WITH orders AS (SELECT * FROM public.orders WHERE false) SELECT * FROM public.orders"
    assert _from_sources(sql) == [("table", "orders", None)]


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


def test_preview_table_query_quotes_plain_identifier():
    query = _preview_table_query("customers").as_string(None)
    assert query == 'SELECT * FROM "customers" LIMIT %s'


def test_preview_table_query_escapes_embedded_quote_in_identifier():
    # テーブル名に二重引用符が含まれていても、SQLとして安全に(引用符を
    # 2つ並べてエスケープした形で)組み立てられることを確認する
    query = _preview_table_query('weird"table').as_string(None)
    assert query == 'SELECT * FROM "weird""table" LIMIT %s'


class _RecordingCursor:
    """preview_source_tablesが発行するSQLとパラメーターを記録する、DB接続不要の代用品"""

    description = ()

    def __init__(self, calls: list) -> None:
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._calls.append((query, params))

    def fetchall(self) -> list:
        return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.calls)


@pytest.fixture
def recording_connection(monkeypatch) -> _RecordingConnection:
    conn = _RecordingConnection()
    monkeypatch.setattr("app.sql_steps.get_connection", lambda: conn)
    return conn


def test_preview_passes_no_params_for_generated_cte_and_subquery_sql(recording_connection):
    # 空のタプルを渡すと、psycopgがLIKEの`%`をプレースホルダーとして解析して失敗する
    # (`%`の直後が日本語の場合はUnicodeDecodeErrorになる)ため、Noneを渡す
    preview_source_tables("""
        WITH t AS (SELECT * FROM customers WHERE name LIKE '%子')
        SELECT * FROM t
        JOIN (SELECT * FROM orders WHERE status LIKE '完%') AS o ON t.customer_id = o.customer_id
        JOIN products p ON true
    """)
    params = [p for _query, p in recording_connection.calls]
    assert params[:2] == [None, None]
    assert params[2] is not None


def test_preview_of_derived_table_with_own_with_clause_keeps_outer_cte(recording_connection):
    preview_source_tables("""
        WITH a AS (SELECT customer_id FROM customers)
        SELECT * FROM (WITH b AS (SELECT * FROM a) SELECT * FROM b) AS sub
    """)
    [(query, _params)] = recording_connection.calls
    assert "WITH a AS" in query
    assert "WITH b AS" in query


# --- CTE(WITH句) ---------------------------------------------------------


def test_cte_adds_with_step_before_from_join():
    sql = "WITH recent AS (SELECT * FROM orders WHERE status = '完了') SELECT * FROM recent"
    assert _labels(sql) == ["WITH recent", "FROM / JOIN", "SELECT"]


def test_multiple_ctes_each_get_their_own_step_in_order():
    sql = """
        WITH a AS (SELECT * FROM customers), b AS (SELECT * FROM orders)
        SELECT * FROM a JOIN b ON a.customer_id = b.customer_id
    """
    assert _labels(sql) == ["WITH a", "WITH b", "FROM / JOIN", "SELECT"]


def test_cte_step_sql_is_self_contained_with_with_clause():
    sql = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    sqls = _sql_by_label(sql)
    assert sqls["WITH recent"].upper().startswith("WITH")
    assert "recent" in sqls["WITH recent"]


def test_steps_after_cte_still_include_with_clause_for_standalone_execution():
    sql = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent WHERE order_id > 1"
    sqls = _sql_by_label(sql)
    assert sqls["WHERE"].upper().startswith("WITH")


# --- サブクエリ -------------------------------------------------------------


def test_scalar_subquery_in_select_is_supported():
    sql = """
        SELECT c.customer_id,
               (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.customer_id) AS order_count
        FROM customers c
    """
    assert _labels(sql) == ["FROM / JOIN", "SELECT"]


def test_in_subquery_in_where_is_supported():
    sql = """
        SELECT * FROM customers
        WHERE customer_id IN (SELECT customer_id FROM orders WHERE status = '完了')
    """
    assert _labels(sql) == ["FROM / JOIN", "WHERE", "SELECT"]


def test_exists_correlated_subquery_is_supported():
    sql = """
        SELECT * FROM orders o
        WHERE EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.order_id)
    """
    assert _labels(sql) == ["FROM / JOIN", "WHERE", "SELECT"]


def test_derived_table_in_from_is_supported():
    sql = "SELECT * FROM (SELECT * FROM orders WHERE status = '完了') AS recent_orders"
    assert _labels(sql) == ["FROM / JOIN", "SELECT"]


def test_where_highlight_excludes_columns_only_referenced_inside_subquery():
    sql = """
        SELECT * FROM orders o
        WHERE o.status = '完了'
          AND o.customer_id IN (SELECT customer_id FROM customers WHERE prefecture = '東京都')
    """
    highlights = _highlights_by_label(sql)
    assert highlights["WHERE"] == ["status", "customer_id"]
    assert "prefecture" not in highlights["WHERE"]


# --- ウィンドウ関数 ---------------------------------------------------------


def test_window_function_adds_window_step_before_select():
    sql = "SELECT customer_id, ROW_NUMBER() OVER (ORDER BY customer_id) AS rn FROM customers"
    assert _labels(sql) == ["FROM / JOIN", "WINDOW", "SELECT"]


def test_window_step_positioned_after_having_and_before_order_by():
    sql = """
        SELECT customer_id, COUNT(*) OVER (PARTITION BY customer_id) AS cnt
        FROM orders
        WHERE status = '完了'
        GROUP BY customer_id
        HAVING COUNT(*) > 0
        ORDER BY cnt
    """
    assert _labels(sql) == [
        "FROM / JOIN", "WHERE", "GROUP BY", "HAVING", "WINDOW", "SELECT", "ORDER BY",
    ]


def test_window_highlight_columns_from_partition_and_order_by():
    sql = "SELECT customer_id, RANK() OVER (PARTITION BY prefecture ORDER BY created_at) AS r FROM customers"
    highlights = _highlights_by_label(sql)
    assert highlights["WINDOW"] == ["prefecture", "created_at"]


def test_window_function_inside_subquery_does_not_trigger_top_level_window_step():
    sql = """
        SELECT c.customer_id,
               (SELECT COUNT(*) OVER () FROM orders o WHERE o.customer_id = c.customer_id LIMIT 1) AS cnt
        FROM customers c
    """
    assert "WINDOW" not in _labels(sql)


def test_window_function_is_not_evaluated_before_window_step():
    # ウィンドウ関数はHAVINGの後に評価されるため、GROUP BY/HAVINGステップには含めない
    sql = """
        SELECT customer_id, COUNT(*) OVER () AS groups
        FROM orders
        GROUP BY customer_id
        HAVING COUNT(*) > 1
    """
    sqls = _sql_by_label(sql)
    assert "OVER" not in sqls["GROUP BY"]
    assert "OVER" not in sqls["HAVING"]
    assert "OVER" in sqls["WINDOW"]


def test_group_by_step_falls_back_to_group_columns_when_all_targets_are_windows():
    sql = "SELECT COUNT(*) OVER () AS groups FROM orders GROUP BY customer_id"
    sqls = _sql_by_label(sql)
    assert sqls["GROUP BY"] == "SELECT customer_id FROM orders GROUP BY customer_id"


def test_window_step_includes_window_function_only_in_order_by():
    sql = "SELECT name FROM customers ORDER BY ROW_NUMBER() OVER (PARTITION BY prefecture ORDER BY customer_id)"
    sqls = _sql_by_label(sql)
    highlights = _highlights_by_label(sql)
    assert "OVER" in sqls["WINDOW"]
    assert "OVER" not in sqls["SELECT"]
    assert highlights["WINDOW"] == ["prefecture", "customer_id"]


def test_window_step_shows_partition_and_order_columns_missing_from_select():
    # ハイライト対象の列がSELECT句に無いと何も光らないため、WINDOWステップでだけ表示する
    sql = "SELECT RANK() OVER (PARTITION BY customer_id ORDER BY order_date) AS r FROM orders"
    sqls = _sql_by_label(sql)
    assert sqls["WINDOW"] == (
        "SELECT customer_id, order_date, rank() OVER (PARTITION BY customer_id ORDER BY order_date) AS r "
        "FROM orders"
    )
    assert sqls["SELECT"] == "SELECT rank() OVER (PARTITION BY customer_id ORDER BY order_date) AS r FROM orders"


def test_window_step_does_not_repeat_selected_columns_or_add_expressions():
    sql = """
        SELECT customer_id,
               RANK() OVER (PARTITION BY customer_id ORDER BY date_trunc('month', order_date)) AS r
        FROM orders
    """
    sqls = _sql_by_label(sql)
    assert sqls["WINDOW"] == sqls["SELECT"]


def test_window_step_adds_no_input_columns_when_selecting_star():
    sql = "SELECT *, RANK() OVER (PARTITION BY customer_id ORDER BY order_date) AS r FROM orders"
    sqls = _sql_by_label(sql)
    assert sqls["WINDOW"] == sqls["SELECT"]


# --- UNION / INTERSECT / EXCEPT --------------------------------------------


def test_union_produces_left_right_and_combine_steps():
    sql = """
        SELECT customer_id FROM customers WHERE prefecture = '東京都'
        UNION
        SELECT customer_id FROM customers WHERE prefecture = '大阪府'
    """
    assert _labels(sql) == ["1つ目のSELECT", "2つ目のSELECT", "UNION"]


def test_union_all_label():
    sql = "SELECT customer_id FROM customers UNION ALL SELECT customer_id FROM orders"
    assert "UNION ALL" in _labels(sql)


def test_intersect_and_except_labels():
    assert "INTERSECT" in _labels(
        "SELECT customer_id FROM customers INTERSECT SELECT customer_id FROM orders"
    )
    assert "EXCEPT" in _labels(
        "SELECT customer_id FROM customers EXCEPT SELECT customer_id FROM orders"
    )


def test_union_with_order_by_and_limit_adds_trailing_steps():
    sql = """
        SELECT customer_id FROM customers
        UNION
        SELECT customer_id FROM orders
        ORDER BY customer_id
        LIMIT 5
    """
    assert _labels(sql) == ["1つ目のSELECT", "2つ目のSELECT", "UNION", "ORDER BY", "LIMIT / OFFSET"]


def test_union_arm_sql_is_independently_executable():
    sql = "SELECT customer_id FROM customers UNION SELECT customer_id FROM orders"
    sqls = _sql_by_label(sql)
    assert "customers" in sqls["1つ目のSELECT"]
    assert "orders" in sqls["2つ目のSELECT"]


def test_union_arm_with_own_with_clause_keeps_outer_cte_scope():
    # 枝自身のWITH句(b)が外側のCTE(a)を参照していても、枝を単独実行できること
    sql = """
        WITH a AS (SELECT customer_id FROM customers)
        (WITH b AS (SELECT * FROM a) SELECT * FROM b)
        UNION
        SELECT customer_id FROM a
    """
    sqls = _sql_by_label(sql)
    assert sqls["1つ目のSELECT"] == (
        "WITH a AS (SELECT customer_id FROM customers) "
        "SELECT * FROM (WITH b AS (SELECT * FROM a) SELECT * FROM b) AS arm"
    )


def test_union_with_cte_propagates_with_clause_to_arms():
    sql = """
        WITH recent AS (SELECT customer_id FROM orders WHERE status = '完了')
        SELECT customer_id FROM recent
        UNION
        SELECT customer_id FROM customers
    """
    sqls = _sql_by_label(sql)
    assert sqls["1つ目のSELECT"].upper().startswith("WITH")
    assert sqls["2つ目のSELECT"].upper().startswith("WITH")
