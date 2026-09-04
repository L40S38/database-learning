"""SELECT文を句の境界ごとに分解し、各ステップまでのSQLを本物のPostgreSQLに
実行させて中間結果を取得するロジック。

独自の実行エンジンを実装するのではなく、「そのステップまでのSQL」を
pglast(libpg_queryのPythonバインディング)でASTから組み立てて
本物のPostgreSQLに投げることで、正確な実行結果を段階的に見せる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from pglast import parse_sql
from pglast.ast import A_Star, ColumnRef, JoinExpr, RangeVar, ResTarget, SelectStmt
from pglast.parser import ParseError
from pglast.stream import RawStream
from pglast.visitors import Visitor
from psycopg.sql import SQL, Composed, Identifier

from .db import get_connection

# 表示・プレビュー実行時に取得する最大行数(サンプルデータ規模を大きく超える
# 入力があっても応答を軽くするための保険)
MAX_PREVIEW_ROWS = 200


class UnsupportedQueryError(ValueError):
    """MVPのスコープ外のSQLが入力された場合の例外"""


@dataclass
class StepResult:
    label: str
    description: str
    sql: str
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    row_count: int | None = None
    error: str | None = None
    highlight_columns: list[str] = field(default_factory=list)


@dataclass
class TablePreview:
    table: str
    alias: str | None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    row_count: int | None = None
    error: str | None = None


class _ColumnNameFinder(Visitor):
    """ASTの部分木からColumnRefの列名(テーブル修飾子を除く)を出現順・重複なしで集める"""

    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_ColumnRef(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        last_field = node.fields[-1]
        name = getattr(last_field, "sval", None)
        if name and name not in self.names:
            self.names.append(name)


def _referenced_column_names(node: Any) -> list[str]:
    """指定した句(WHERE/GROUP BY/HAVING/ORDER BY/JOIN条件など)が参照している
    列名を、そのステップの「根拠となる列」としてハイライト表示するために抽出する。
    """
    if not node:
        return []
    finder = _ColumnNameFinder()
    finder(node)
    return finder.names


class _WindowFunctionDetector(Visitor):
    """ASTの部分木にウィンドウ関数呼び出し(`OVER`句を伴う関数呼び出し)が
    含まれるかを調べる。ウィンドウ関数はGROUP BY/HAVINGの後・DISTINCT/ORDER BYの
    前という独自の評価順序を持つが、現状のステップ分解モデルにはその評価順序が
    組み込まれていないため、含まれていた場合は明示的に未対応として拒否する
    (黙って評価順序が崩れたSQLを実行してしまうと、見た目上は結果が返るのに
    意味が変わってしまうため)。
    """

    def __init__(self) -> None:
        self.found = False

    def visit_FuncCall(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        if node.over is not None:
            self.found = True


def _has_window_function(node: Any) -> bool:
    if not node:
        return False
    detector = _WindowFunctionDetector()
    detector(node)
    return detector.found


def _star_target_list() -> tuple[ResTarget, ...]:
    return (ResTarget(val=ColumnRef(fields=(A_Star(),))),)


def _deparse(node: SelectStmt) -> str:
    return RawStream()(node)


def _build_step_nodes(select: SelectStmt) -> list[tuple[str, str, SelectStmt, list[str]]]:
    """(ラベル, 説明, そのステップまでを表すSelectStmt, ハイライトすべき列名) の
    リストを組み立てる。

    実行順序(論理句の評価順)に沿って、元のクエリに存在する句だけを
    ステップとして積み上げていく。ハイライト列は、そのステップで新たに
    加わった句が参照している列(=そのステップの判断根拠になった列)。
    """
    star = _star_target_list()
    steps: list[tuple[str, str, SelectStmt, list[str]]] = []

    steps.append((
        "FROM / JOIN",
        "対象のテーブルを結合し、絞り込み前の全行を取得します",
        SelectStmt(targetList=star, fromClause=select.fromClause),
        _referenced_column_names(select.fromClause),
    ))

    if select.whereClause is not None:
        steps.append((
            "WHERE",
            "条件に一致する行だけに絞り込みます",
            SelectStmt(
                targetList=star,
                fromClause=select.fromClause,
                whereClause=select.whereClause,
            ),
            _referenced_column_names(select.whereClause),
        ))

    if select.groupClause:
        steps.append((
            "GROUP BY",
            "指定した列の値でグループ化し、集約関数を計算します",
            SelectStmt(
                targetList=select.targetList,
                fromClause=select.fromClause,
                whereClause=select.whereClause,
                groupClause=select.groupClause,
            ),
            _referenced_column_names(select.groupClause),
        ))

    if select.havingClause is not None:
        steps.append((
            "HAVING",
            "グループ化した結果をさらに条件で絞り込みます",
            SelectStmt(
                targetList=select.targetList,
                fromClause=select.fromClause,
                whereClause=select.whereClause,
                groupClause=select.groupClause,
                havingClause=select.havingClause,
            ),
            _referenced_column_names(select.havingClause),
        ))

    is_distinct = select.distinctClause is not None
    steps.append((
        "SELECT DISTINCT" if is_distinct else "SELECT",
        "出力する列を選択します" + ("(重複する行は除去されます)" if is_distinct else ""),
        SelectStmt(
            targetList=select.targetList,
            distinctClause=select.distinctClause,
            fromClause=select.fromClause,
            whereClause=select.whereClause,
            groupClause=select.groupClause,
            havingClause=select.havingClause,
        ),
        [],
    ))

    if select.sortClause:
        steps.append((
            "ORDER BY",
            "指定した順序に並び替えます",
            SelectStmt(
                targetList=select.targetList,
                distinctClause=select.distinctClause,
                fromClause=select.fromClause,
                whereClause=select.whereClause,
                groupClause=select.groupClause,
                havingClause=select.havingClause,
                sortClause=select.sortClause,
            ),
            _referenced_column_names(select.sortClause),
        ))

    if select.limitCount is not None or select.limitOffset is not None:
        steps.append((
            "LIMIT / OFFSET",
            "件数を絞り込みます",
            select,
            [],
        ))

    return steps


def _parse_select_statement(sql: str) -> SelectStmt:
    """SQLを解析し、MVPで対応するSELECT文かどうかを検証したうえでASTを返す"""
    try:
        parsed = parse_sql(sql)
    except ParseError as exc:
        raise UnsupportedQueryError(f"SQLの構文解析に失敗しました: {exc}") from exc

    if len(parsed) != 1:
        raise UnsupportedQueryError("1つのSELECT文のみ入力してください")

    stmt = parsed[0].stmt
    if not isinstance(stmt, SelectStmt):
        raise UnsupportedQueryError("SELECT文のみ対応しています")
    if stmt.larg is not None or stmt.rarg is not None:
        raise UnsupportedQueryError("UNION/INTERSECT/EXCEPTは未対応です")
    if stmt.withClause is not None:
        raise UnsupportedQueryError("WITH句(CTE)は未対応です")
    if stmt.valuesLists:
        raise UnsupportedQueryError("VALUES句のみのSELECT文は未対応です")
    if not stmt.fromClause:
        raise UnsupportedQueryError("FROM句のないSELECT文は未対応です")
    if stmt.lockingClause:
        raise UnsupportedQueryError("FOR UPDATE/FOR SHAREなどのロック句は未対応です")
    if stmt.windowClause or _has_window_function(stmt):
        raise UnsupportedQueryError("ウィンドウ関数(OVER句)は未対応です")

    return stmt


def parse_to_steps(sql: str) -> list[tuple[str, str, str, list[str]]]:
    """SQLを解析し、(ラベル, 説明, そのステップのSQL文字列, ハイライト列名) の
    リストを返す"""
    stmt = _parse_select_statement(sql)
    return [
        (label, desc, _deparse(node), highlight)
        for label, desc, node, highlight in _build_step_nodes(stmt)
    ]


def _extract_base_tables(from_clause: tuple) -> list[tuple[str, str | None]]:
    """fromClauseに登場する実テーブルを (テーブル名, エイリアス) として
    左から出現順に再帰的に抽出する。サブクエリ/関数など実テーブルでない
    要素は対象外(このMVPではプレビュー不可としてスキップする)。
    """
    tables: list[tuple[str, str | None]] = []

    def walk(node: object) -> None:
        if isinstance(node, RangeVar):
            alias = node.alias.aliasname if node.alias is not None else None
            tables.append((node.relname, alias))
        elif isinstance(node, JoinExpr):
            walk(node.larg)
            walk(node.rarg)

    for item in from_clause:
        walk(item)

    return tables


def _preview_table_query(relname: str) -> Composed:
    """指定したテーブル名を安全にクォートした、ステップ0プレビュー用のSELECT文を
    組み立てる(テーブル名を生文字列でSQLに埋め込まないようにするため)。
    """
    return SQL("SELECT * FROM {} LIMIT %s").format(Identifier(relname))


def preview_source_tables(sql: str) -> tuple[list[TablePreview], list[str]]:
    """FROM句が参照している各テーブルの、絞り込み前の生データを取得する。

    「実行」ボタンを押す前でも表示できる、実行前の状態(ステップ0)用。
    あわせて、直後のFROM/JOINステップで根拠になる列名(JOIN条件の列)も
    返し、実行前の時点から「次にどの列が使われるか」を示せるようにする。
    """
    stmt = _parse_select_statement(sql)
    tables = _extract_base_tables(stmt.fromClause)
    if not tables:
        raise UnsupportedQueryError("対象テーブルを特定できませんでした")

    highlight_columns = _referenced_column_names(stmt.fromClause)

    results: list[TablePreview] = []
    with get_connection() as conn:
        for relname, alias in tables:
            preview_sql = _preview_table_query(relname)
            try:
                with conn.cursor() as cur:
                    cur.execute(preview_sql, (MAX_PREVIEW_ROWS,))
                    columns = [c.name for c in cur.description]
                    rows = [[_serialize(v) for v in row] for row in cur.fetchall()]
            except psycopg.Error as exc:
                conn.rollback()
                results.append(TablePreview(table=relname, alias=alias, error=str(exc).strip()))
            else:
                results.append(TablePreview(
                    table=relname, alias=alias,
                    columns=columns, rows=rows, row_count=len(rows),
                ))

    return results, highlight_columns


def _serialize(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def execute_steps(sql: str) -> list[StepResult]:
    """各ステップのSQLを実際にPostgreSQLへ発行し、結果を添えて返す。

    途中のステップで実行エラーになった場合は、それ以降のステップは
    実行せずエラー内容を添えて打ち切る(以降も同じ原因で失敗するため)。
    """
    step_defs = parse_to_steps(sql)
    results: list[StepResult] = []

    with get_connection() as conn:
        for label, desc, step_sql, highlight in step_defs:
            preview_sql = f"SELECT * FROM ({step_sql}) AS step_preview LIMIT {MAX_PREVIEW_ROWS}"
            try:
                with conn.cursor() as cur:
                    cur.execute(preview_sql)
                    columns = [c.name for c in cur.description]
                    rows = [[_serialize(v) for v in row] for row in cur.fetchall()]
            except psycopg.Error as exc:
                conn.rollback()
                results.append(StepResult(
                    label=label, description=desc, sql=step_sql,
                    error=str(exc).strip(), highlight_columns=highlight,
                ))
                break
            else:
                results.append(StepResult(
                    label=label, description=desc, sql=step_sql,
                    columns=columns, rows=rows, row_count=len(rows),
                    highlight_columns=highlight,
                ))

    return results
