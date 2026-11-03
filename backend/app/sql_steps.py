"""SELECT文を句の境界ごとに分解し、各ステップまでのSQLを本物のPostgreSQLに
実行させて中間結果を取得するロジック。

独自の実行エンジンを実装するのではなく、「そのステップまでのSQL」を
pglast(libpg_queryのPythonバインディング)でASTから組み立てて
本物のPostgreSQLに投げることで、正確な実行結果を段階的に見せる。

対応構文: 単一SELECT(FROM/JOIN・WHERE・GROUP BY・HAVING・ウィンドウ関数・
DISTINCT・ORDER BY・LIMIT/OFFSET)、非再帰CTE(WITH句)、サブクエリ(スカラ・
IN/EXISTS・FROM句内の派生テーブル・相関サブクエリ)、2項のUNION/INTERSECT/
EXCEPT。CTE本体・UNIONの各枝・派生テーブルは、それ自体をさらに句ごとに
分解するのではなく「計算済みの1つの結果」を返す1ステップとして扱う
(このアプリの可視化の粒度は「トップレベルの1つのSELECT文」を単位とする)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from pglast import parse_sql
from pglast.ast import (
    A_Star,
    ColumnRef,
    JoinExpr,
    RangeSubselect,
    RangeVar,
    ResTarget,
    SelectStmt,
)
from pglast.enums import SetOperation
from pglast.parser import ParseError
from pglast.stream import RawStream
from pglast.visitors import Skip, Visitor
from psycopg.sql import SQL, Composed, Identifier

from .db import get_connection

# 表示・プレビュー実行時に取得する最大行数(サンプルデータ規模を大きく超える
# 入力があっても応答を軽くするための保険)
MAX_PREVIEW_ROWS = 200


class UnsupportedQueryError(ValueError):
    """このアプリのスコープ外のSQLが入力された場合の例外"""


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


@dataclass
class _FromSource:
    """FROM句の1要素を分類した結果(ステップ0のプレビュー用)。

    - table: 実テーブル(relnameをそのままDBに問い合わせる)
    - cte: CTE(WITH句)の参照。CTE本体を単独実行できるよう、元のWITH句ごと
      組み立て直して実行する
    - subquery: FROM句内の派生テーブル(サブクエリ)。サブクエリ自体を
      単独実行できる形で実行する
    """

    kind: str
    alias: str | None
    relname: str | None = None
    subquery: SelectStmt | None = None


class _ColumnNameFinder(Visitor):
    """ASTの部分木からColumnRefの列名(テーブル修飾子を除く)を出現順・重複なしで集める。

    ネストしたSelectStmt(スカラサブクエリ・IN/EXISTSの中身など)には descend
    しない。サブクエリ内部で参照されている列は、外側の句自身の「判断根拠になった
    列」とは言えないため、ハイライト対象から除外する。
    """

    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_SelectStmt(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        return Skip

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
    トップレベルで含まれるかを調べる(ネストしたSelectStmtの中は対象外。
    サブクエリ内のウィンドウ関数は、そのサブクエリ自身の評価の話であり、
    外側のクエリにWINDOWステップを追加する理由にはならないため)。
    """

    def __init__(self) -> None:
        self.found = False

    def visit_SelectStmt(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        return Skip

    def visit_FuncCall(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        if node.over is not None:
            self.found = True


def _has_window_function(node: Any) -> bool:
    if not node:
        return False
    detector = _WindowFunctionDetector()
    detector(node)
    return detector.found


class _WindowColumnFinder(Visitor):
    """トップレベルのウィンドウ関数呼び出しについて、`OVER`句の
    PARTITION BY/ORDER BYで参照されている列名を出現順・重複なしで集める。
    """

    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_SelectStmt(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        return Skip

    def visit_FuncCall(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        if node.over is None:
            return
        for name in _referenced_column_names(node.over.partitionClause):
            if name not in self.names:
                self.names.append(name)
        for name in _referenced_column_names(node.over.orderClause):
            if name not in self.names:
                self.names.append(name)


def _window_highlight_columns(node: Any) -> list[str]:
    if not node:
        return []
    finder = _WindowColumnFinder()
    finder(node)
    return finder.names


def _star_target_list() -> tuple[ResTarget, ...]:
    return (ResTarget(val=ColumnRef(fields=(A_Star(),))),)


def _deparse(node: SelectStmt) -> str:
    return RawStream()(node)


def _clone_with(node: SelectStmt, **overrides: Any) -> SelectStmt:
    """SelectStmtノードを、一部フィールドだけ上書きした新しいノードとして複製する。

    CTE本体・UNIONの各枝など、既存のSelectStmt全体をほぼそのまま使いつつ
    「単独実行できるようトップレベルのWITH句だけ付け直す」といった場面で使う。
    """
    fields = {a: getattr(node, a) for a in node}
    fields.update(overrides)
    return SelectStmt(**fields)


def _cte_names(stmt: SelectStmt) -> frozenset[str]:
    if stmt.withClause is None:
        return frozenset()
    return frozenset(cte.ctename for cte in stmt.withClause.ctes)


def _build_step_nodes(select: SelectStmt) -> list[tuple[str, str, SelectStmt, list[str]]]:
    """(ラベル, 説明, そのステップまでを表すSelectStmt, ハイライトすべき列名) の
    リストを組み立てる(UNION等を伴わない、単一SELECT文の場合)。

    実行順序(論理句の評価順)に沿って、元のクエリに存在する句だけを
    ステップとして積み上げていく。ハイライト列は、そのステップで新たに
    加わった句が参照している列(=そのステップの判断根拠になった列)。

    CTE(WITH句)が使われている場合、各CTEの結果を先に計算するステップを
    先頭に積む。以降の各ステップのSQLには、CTE名を参照できるよう元の
    WITH句をそのまま付与する(そのステップ単体を実行してもCTE名が解決できる
    ようにするため)。
    """
    star = _star_target_list()
    with_clause = select.withClause
    steps: list[tuple[str, str, SelectStmt, list[str]]] = []

    if with_clause is not None:
        for cte in with_clause.ctes:
            cte_preview = SelectStmt(
                targetList=star,
                fromClause=(RangeVar(relname=cte.ctename),),
                withClause=with_clause,
            )
            steps.append((
                f"WITH {cte.ctename}",
                f"CTE「{cte.ctename}」の結果を計算します",
                cte_preview,
                [],
            ))

    steps.append((
        "FROM / JOIN",
        "対象のテーブルを結合し、絞り込み前の全行を取得します",
        SelectStmt(targetList=star, fromClause=select.fromClause, withClause=with_clause),
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
                withClause=with_clause,
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
                withClause=with_clause,
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
                withClause=with_clause,
            ),
            _referenced_column_names(select.havingClause),
        ))

    has_window = _has_window_function(select.targetList) or _has_window_function(select.sortClause)
    if has_window:
        steps.append((
            "WINDOW",
            "ウィンドウ関数(OVER句)を計算します。行を集約せず、各行についてパーティション内の順位・累計などを算出します",
            SelectStmt(
                targetList=select.targetList,
                fromClause=select.fromClause,
                whereClause=select.whereClause,
                groupClause=select.groupClause,
                havingClause=select.havingClause,
                withClause=with_clause,
            ),
            _window_highlight_columns(select.targetList),
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
            withClause=with_clause,
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
                withClause=with_clause,
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


_SETOP_LABELS = {
    SetOperation.SETOP_UNION: "UNION",
    SetOperation.SETOP_INTERSECT: "INTERSECT",
    SetOperation.SETOP_EXCEPT: "EXCEPT",
}

_SETOP_DESCRIPTIONS = {
    SetOperation.SETOP_UNION: "2つの結果を結合します(重複する行は除去されます)",
    SetOperation.SETOP_INTERSECT: "両方の結果に共通する行だけを残します",
    SetOperation.SETOP_EXCEPT: "1つ目の結果から、2つ目の結果に含まれる行を取り除きます",
}
_SETOP_DESCRIPTIONS_ALL = {
    SetOperation.SETOP_UNION: "2つの結果をそのまま結合します(重複する行も残ります)",
    SetOperation.SETOP_INTERSECT: "両方の結果に共通する行を残します(重複する行も考慮します)",
    SetOperation.SETOP_EXCEPT: "1つ目の結果から、2つ目の結果に含まれる行を取り除きます(重複する行も考慮します)",
}


def _standalone_arm(arm: SelectStmt, with_clause: Any) -> SelectStmt:
    """UNION等の片側(larg/rarg)を、単独実行してもCTE名が解決できるように
    トップレベルのWITH句を付与する(片側が独自にWITH句を持つ場合はそちらを優先する)。
    """
    if arm.withClause is not None or with_clause is None:
        return arm
    return _clone_with(arm, withClause=with_clause)


def _build_setop_step_nodes(stmt: SelectStmt) -> list[tuple[str, str, SelectStmt, list[str]]]:
    """UNION/INTERSECT/EXCEPTのステップを組み立てる。

    左右それぞれの枝は、句ごとに分解するのではなく「その枝を丸ごと実行した結果」
    を1ステップとして表示する(CTEやFROM句の派生テーブルと同じ考え方)。
    その後、両方を結合するステップ、続いてトップレベルのORDER BY/LIMITが
    あればそれぞれ追加する。
    """
    label = _SETOP_LABELS[stmt.op] + (" ALL" if stmt.all else "")
    description = (_SETOP_DESCRIPTIONS_ALL if stmt.all else _SETOP_DESCRIPTIONS)[stmt.op]

    steps: list[tuple[str, str, SelectStmt, list[str]]] = [
        (
            "1つ目のSELECT",
            "1つ目のSELECT文を実行します",
            _standalone_arm(stmt.larg, stmt.withClause),
            [],
        ),
        (
            "2つ目のSELECT",
            "2つ目のSELECT文を実行します",
            _standalone_arm(stmt.rarg, stmt.withClause),
            [],
        ),
        (
            label,
            description,
            _clone_with(stmt, sortClause=None, limitOffset=None, limitCount=None),
            [],
        ),
    ]

    if stmt.sortClause:
        steps.append((
            "ORDER BY",
            "指定した順序に並び替えます",
            _clone_with(stmt, limitOffset=None, limitCount=None),
            _referenced_column_names(stmt.sortClause),
        ))

    if stmt.limitCount is not None or stmt.limitOffset is not None:
        steps.append((
            "LIMIT / OFFSET",
            "件数を絞り込みます",
            stmt,
            [],
        ))

    return steps


def _parse_select_statement(sql: str) -> SelectStmt:
    """SQLを解析し、対応するSELECT文かどうかを検証したうえでASTを返す"""
    try:
        parsed = parse_sql(sql)
    except ParseError as exc:
        raise UnsupportedQueryError(f"SQLの構文解析に失敗しました: {exc}") from exc

    if len(parsed) != 1:
        raise UnsupportedQueryError("1つのSELECT文のみ入力してください")

    stmt = parsed[0].stmt
    if not isinstance(stmt, SelectStmt):
        raise UnsupportedQueryError("SELECT文のみ対応しています")
    if stmt.valuesLists:
        raise UnsupportedQueryError("VALUES句のみのSELECT文は未対応です")
    if stmt.lockingClause:
        raise UnsupportedQueryError("FOR UPDATE/FOR SHAREなどのロック句は未対応です")

    if stmt.op != SetOperation.SETOP_NONE:
        if stmt.larg.op != SetOperation.SETOP_NONE or stmt.rarg.op != SetOperation.SETOP_NONE:
            raise UnsupportedQueryError("3つ以上のUNION/INTERSECT/EXCEPTの連結は未対応です")
        return stmt

    if stmt.withClause is not None and stmt.withClause.recursive:
        raise UnsupportedQueryError("再帰CTE(WITH RECURSIVE)は未対応です")
    if not stmt.fromClause:
        raise UnsupportedQueryError("FROM句のないSELECT文は未対応です")
    if stmt.windowClause:
        raise UnsupportedQueryError("名前付きWINDOW句(WINDOW ... AS (...))は未対応です")

    return stmt


def parse_to_steps(sql: str) -> list[tuple[str, str, str, list[str]]]:
    """SQLを解析し、(ラベル, 説明, そのステップのSQL文字列, ハイライト列名) の
    リストを返す"""
    stmt = _parse_select_statement(sql)
    if stmt.op != SetOperation.SETOP_NONE:
        nodes = _build_setop_step_nodes(stmt)
    else:
        nodes = _build_step_nodes(stmt)
    return [(label, desc, _deparse(node), highlight) for label, desc, node, highlight in nodes]


def _extract_from_sources(from_clause: tuple, cte_names: frozenset[str]) -> list[_FromSource]:
    """fromClauseに登場する各要素を、実テーブル/CTE参照/FROM句内の派生テーブル
    (サブクエリ)に分類し、左から出現順に再帰的に抽出する(ステップ0のプレビュー用)。
    """
    sources: list[_FromSource] = []

    def walk(node: object) -> None:
        if isinstance(node, RangeVar):
            alias = node.alias.aliasname if node.alias is not None else None
            if node.relname in cte_names:
                sources.append(_FromSource(kind="cte", alias=alias, relname=node.relname))
            else:
                sources.append(_FromSource(kind="table", alias=alias, relname=node.relname))
        elif isinstance(node, JoinExpr):
            walk(node.larg)
            walk(node.rarg)
        elif isinstance(node, RangeSubselect):
            alias = node.alias.aliasname if node.alias is not None else None
            sources.append(_FromSource(kind="subquery", alias=alias, subquery=node.subquery))

    for item in from_clause:
        walk(item)

    return sources


def _preview_table_query(relname: str) -> Composed:
    """指定したテーブル名を安全にクォートした、ステップ0プレビュー用のSELECT文を
    組み立てる(テーブル名を生文字列でSQLに埋め込まないようにするため)。
    """
    return SQL("SELECT * FROM {} LIMIT %s").format(Identifier(relname))


def preview_source_tables(sql: str) -> tuple[list[TablePreview], list[str]]:
    """FROM句が参照している各要素の、絞り込み前の生データを取得する。

    「実行」ボタンを押す前でも表示できる、実行前の状態(ステップ0)用。
    実テーブルはそのままSELECTし、CTE参照・FROM句内の派生テーブルは、
    単独実行できる形に組み立て直したうえでSELECTする(いずれも「まだ
    絞り込まれていない、その要素の中身」を見せる)。

    単一SELECTの場合は、あわせて直後のFROM/JOINステップで根拠になる列名
    (JOIN条件の列)も返す。UNION等の場合は左右2つのSELECT文それぞれの
    FROM句をまとめて対象にし、ハイライトは対象外とする(「次のステップ」が
    句単位の絞り込みではなく、枝ごとの実行そのものになるため)。
    """
    stmt = _parse_select_statement(sql)
    star = _star_target_list()
    cte_names = _cte_names(stmt)

    if stmt.op != SetOperation.SETOP_NONE:
        sources = [
            source
            for arm in (stmt.larg, stmt.rarg)
            for source in _extract_from_sources(arm.fromClause, cte_names)
        ]
        highlight_columns: list[str] = []
    else:
        sources = _extract_from_sources(stmt.fromClause, cte_names)
        highlight_columns = _referenced_column_names(stmt.fromClause)

    if not sources:
        raise UnsupportedQueryError("対象テーブルを特定できませんでした")

    results: list[TablePreview] = []
    with get_connection() as conn:
        for source in sources:
            if source.kind == "table":
                label, alias = source.relname, source.alias
                preview_sql: Composed | str = _preview_table_query(source.relname)
                params: tuple = (MAX_PREVIEW_ROWS,)
            elif source.kind == "cte":
                label, alias = source.relname, source.alias
                inner = SelectStmt(
                    targetList=star,
                    fromClause=(RangeVar(relname=source.relname),),
                    withClause=stmt.withClause,
                )
                preview_sql = f"SELECT * FROM ({_deparse(inner)}) AS step_preview LIMIT {MAX_PREVIEW_ROWS}"
                params = ()
            else:
                label, alias = source.alias or "(サブクエリ)", source.alias
                inner = _standalone_arm(source.subquery, stmt.withClause)
                preview_sql = f"SELECT * FROM ({_deparse(inner)}) AS step_preview LIMIT {MAX_PREVIEW_ROWS}"
                params = ()

            try:
                with conn.cursor() as cur:
                    cur.execute(preview_sql, params)
                    columns = [c.name for c in cur.description]
                    rows = [[_serialize(v) for v in row] for row in cur.fetchall()]
            except psycopg.Error as exc:
                conn.rollback()
                results.append(TablePreview(table=label, alias=alias, error=str(exc).strip()))
            else:
                results.append(TablePreview(
                    table=label, alias=alias,
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
