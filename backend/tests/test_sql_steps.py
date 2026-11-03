import pytest

from app.sql_steps import (
    UnsupportedQueryError,
    _cte_names,
    _dml_sources,
    _extract_from_sources,
    _parse_statement,
    _plan_steps,
    _preview_table_query,
    parse_to_steps,
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
        "SELECT 1",
        "SELECT * FROM customers; SELECT * FROM orders",
        "CREATE TABLE t (id int)",
        "MERGE INTO customers c USING orders o ON o.customer_id = c.customer_id WHEN MATCHED THEN DELETE",
        "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
        "WITH tokyo AS (SELECT customer_id FROM customers) UPDATE orders SET status = 'x'",
        "UPDATE customers SET name = 'x' WHERE CURRENT OF cur",
        "DELETE FROM customers WHERE CURRENT OF cur",
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
    stmt = _parse_statement(sql)
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


# --- DML(INSERT / UPDATE / DELETE) -----------------------------------------


def _dml_sql_by_label(sql: str, primary_key: tuple[str, ...] = ()) -> dict[str, str]:
    return {label: s for label, _desc, s, _hl in parse_to_steps(sql, primary_key)}


def test_update_steps_target_where_set_after():
    sql = "UPDATE products SET price = price * 2 WHERE category = '食品'"
    assert _labels(sql) == ["対象テーブル", "WHERE", "SET", "実行後のテーブル"]


def test_update_without_where_has_no_where_step():
    assert _labels("UPDATE products SET price = 0") == ["対象テーブル", "SET", "実行後のテーブル"]


def test_update_with_returning_adds_returning_step_before_after_state():
    sql = "UPDATE products SET price = 0 WHERE product_id = 1 RETURNING product_id, price"
    assert _labels(sql) == ["対象テーブル", "WHERE", "SET", "RETURNING", "実行後のテーブル"]


def test_update_with_from_labels_target_step_and_joins_target_with_from_tables():
    sql = """
        UPDATE orders o SET status = 'キャンセル'
        FROM customers c
        WHERE o.customer_id = c.customer_id AND c.prefecture = '大阪府'
    """
    sqls = _sql_by_label(sql)
    assert "対象テーブル + FROM" in sqls
    assert sqls["対象テーブル + FROM"] == "SELECT * FROM orders AS o, customers AS c"
    assert sqls["WHERE"].startswith("SELECT * FROM orders AS o, customers AS c WHERE")


def test_update_highlights_where_columns_and_set_columns():
    sql = """
        UPDATE customers c SET prefecture = '神奈川県', (name, created_at) = ('x', '2024-01-01')
        WHERE c.customer_id = 3
    """
    highlights = _highlights_by_label(sql)
    assert highlights["WHERE"] == ["customer_id"]
    assert highlights["SET"] == ["prefecture", "name", "created_at"]
    assert highlights["実行後のテーブル"] == ["prefecture", "name", "created_at"]


def test_update_set_step_returns_updated_rows_of_target_table_only():
    sqls = _sql_by_label("UPDATE customers c SET name = 'x' WHERE c.customer_id = 1 RETURNING c.name")
    assert sqls["SET"] == "UPDATE customers AS c SET name = 'x' WHERE c.customer_id = 1 RETURNING c.*"
    sqls = _sql_by_label("UPDATE customers SET name = 'x'")
    assert sqls["SET"].endswith("RETURNING customers.*")


def test_after_state_runs_dml_without_returning_then_selects_whole_table_in_primary_key_order():
    sql = "UPDATE customers c SET name = 'x' WHERE c.customer_id = 1 RETURNING c.name"
    sqls = _dml_sql_by_label(sql, primary_key=("customer_id",))
    assert sqls["実行後のテーブル"] == (
        "UPDATE customers AS c SET name = 'x' WHERE c.customer_id = 1;\n"
        "SELECT * FROM customers ORDER BY customer_id"
    )


def test_after_state_is_unordered_without_primary_key():
    sqls = _dml_sql_by_label("DELETE FROM customers")
    assert sqls["実行後のテーブル"].endswith("SELECT * FROM customers")


def test_dml_execution_puts_target_star_before_original_returning():
    stmt = _parse_statement("UPDATE customers c SET name = 'x' RETURNING c.name, now()")
    _steps, execution = _plan_steps(stmt, ("customer_id",))
    assert execution.dml_sql == "UPDATE customers AS c SET name = 'x' RETURNING c.*, c.name, now()"
    assert execution.after_sql == "SELECT * FROM customers ORDER BY customer_id"


def test_select_has_no_dml_execution():
    _steps, execution = _plan_steps(_parse_statement("SELECT * FROM customers"))
    assert execution is None


def test_dml_steps_after_the_first_dml_step_reuse_a_single_execution():
    stmt = _parse_statement("UPDATE products SET price = 0 WHERE product_id = 1 RETURNING price")
    steps, _execution = _plan_steps(stmt)
    assert [s.source for s in steps] == ["query", "query", "affected", "returning", "after"]


def test_delete_steps_target_where_after():
    assert _labels("DELETE FROM orders WHERE status = 'キャンセル'") == [
        "対象テーブル", "WHERE", "実行後のテーブル",
    ]


def test_delete_with_using_and_returning():
    sql = """
        DELETE FROM order_items oi
        USING orders o
        WHERE oi.order_id = o.order_id AND o.status = 'キャンセル'
        RETURNING oi.*
    """
    assert _labels(sql) == ["対象テーブル + USING", "WHERE", "RETURNING", "実行後のテーブル"]
    highlights = _highlights_by_label(sql)
    assert highlights["WHERE"] == ["order_id", "status"]
    assert highlights["実行後のテーブル"] == []


def test_insert_values_steps_and_rows_preview_uses_target_column_names():
    sql = "INSERT INTO customers (name, prefecture, created_at) VALUES ('a', '東京都', '2024-07-01')"
    assert _labels(sql) == ["挿入する行(VALUES)", "INSERT", "実行後のテーブル"]
    sqls = _sql_by_label(sql)
    assert sqls["挿入する行(VALUES)"] == (
        "SELECT * FROM ((VALUES ('a', '東京都', '2024-07-01'))) AS new_rows (name, prefecture, created_at)"
    )
    assert sqls["INSERT"].endswith("RETURNING customers.*")
    assert _highlights_by_label(sql)["INSERT"] == ["name", "prefecture", "created_at"]


def test_insert_select_rows_step():
    sql = "INSERT INTO customers (name, prefecture, created_at) SELECT name, prefecture, created_at FROM customers"
    assert _labels(sql)[0] == "挿入する行(SELECT)"


def test_insert_values_containing_default_skips_rows_preview_step():
    sql = "INSERT INTO customers (name, prefecture, created_at) VALUES ('a', '東京都', DEFAULT)"
    assert _labels(sql) == ["INSERT", "実行後のテーブル"]


def test_insert_default_values_has_no_rows_preview_step():
    assert _labels("INSERT INTO customers DEFAULT VALUES") == ["INSERT", "実行後のテーブル"]


def test_insert_on_conflict_labels_and_highlights():
    do_nothing = "INSERT INTO customers (customer_id, name) VALUES (1, 'a') ON CONFLICT (customer_id) DO NOTHING"
    assert "INSERT ... ON CONFLICT DO NOTHING" in _labels(do_nothing)
    assert _highlights_by_label(do_nothing)["INSERT ... ON CONFLICT DO NOTHING"] == ["customer_id"]

    do_update = """
        INSERT INTO customers (customer_id, name) VALUES (1, 'a')
        ON CONFLICT (customer_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING customer_id
    """
    assert _labels(do_update) == [
        "挿入する行(VALUES)", "INSERT ... ON CONFLICT DO UPDATE", "RETURNING", "実行後のテーブル",
    ]
    assert _highlights_by_label(do_update)["INSERT ... ON CONFLICT DO UPDATE"] == ["customer_id", "name"]


def _dml_source_list(sql: str) -> tuple[list[tuple[str, str | None, str | None]], list[str]]:
    sources, highlight, _with = _dml_sources(_parse_statement(sql))
    return [(s.kind, s.relname, s.alias) for s in sources], highlight


def test_dml_sources_list_target_table_first():
    assert _dml_source_list("UPDATE customers c SET name = 'x'") == ([("table", "customers", "c")], [])
    assert _dml_source_list(
        "UPDATE orders o SET status = 'x' FROM customers c JOIN order_items oi ON oi.order_id = c.customer_id"
    ) == (
        [("table", "orders", "o"), ("table", "customers", "c"), ("table", "order_items", "oi")],
        ["order_id", "customer_id"],
    )
    assert _dml_source_list("DELETE FROM order_items USING orders WHERE true")[0] == [
        ("table", "order_items", None), ("table", "orders", None),
    ]


def test_insert_sources_include_tables_of_select_but_not_values():
    assert _dml_source_list("INSERT INTO customers (name) VALUES ('a')")[0] == [("table", "customers", None)]
    assert _dml_source_list("INSERT INTO customers (name) SELECT name FROM customers c WHERE false")[0] == [
        ("table", "customers", None), ("table", "customers", "c"),
    ]
