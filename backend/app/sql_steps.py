"""SQL文を句の境界ごとに分解し、各ステップまでのSQLを本物のPostgreSQLに
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

INSERT/UPDATE/DELETE(DML)は、対象行の絞り込みまでをSELECTとして段階的に
見せたうえで、DML文そのものは本物のPostgreSQLで1回だけ実行し、その結果
(挿入・更新された行、RETURNINGの出力、実行後のテーブル全体)を後続の
ステップとして見せる。DMLは必ずトランザクション内で実行し、最後にROLLBACK
するため、実際のデータは変更されない。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from pglast import parse_sql
from pglast.ast import (
    A_Star,
    Alias,
    ColumnRef,
    CurrentOfExpr,
    DeleteStmt,
    InsertStmt,
    JoinExpr,
    MergeStmt,
    RangeSubselect,
    RangeVar,
    ResTarget,
    ReturningClause,
    SelectStmt,
    SortBy,
    String,
    UpdateStmt,
)
from pglast.enums import OnConflictAction, SetOperation, SortByDir, SortByNulls
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
class _StepDef:
    """1ステップ分の定義。sourceは、そのステップの結果をどう取得するかを表す。

    - query: sqlをSELECTとしてそのまま(読み取り専用で)実行した結果
    - affected / returning / after: DML文を1回だけ実行した結果から切り出す
      (affected: 挿入・更新された行そのもの、returning: 元のRETURNING句の出力、
      after: 実行後のテーブル全体)。ステップごとにDMLを実行し直さないのは、
      SERIALの採番などROLLBACKしても戻らない副作用でステップ間の値が
      食い違わないようにするため
    """

    label: str
    description: str
    sql: str
    highlight_columns: list[str]
    source: str = "query"


@dataclass
class _DmlExecution:
    """DML文を1回だけ実行して各ステップの結果を切り出すための、実際に投げるSQL。

    dml_sqlのRETURNING句は「対象テーブル.*」を先頭に置き、その後ろに元の
    RETURNING句の式を続けたもの。実行後のテーブル(after_sql)の列数で
    先頭部分(affected)と残り(returning)を切り分ける。
    """

    dml_sql: str
    after_sql: str


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


def _clone_with(node: Any, **overrides: Any) -> Any:
    """ASTノード(SelectStmt・UpdateStmtなど)を、一部フィールドだけ上書きした
    新しいノードとして複製する。

    CTE本体・UNIONの各枝など、既存のSelectStmt全体をほぼそのまま使いつつ
    「単独実行できるようトップレベルのWITH句だけ付け直す」、DML文の
    RETURNING句だけを差し替える、といった場面で使う。
    """
    fields = {a: getattr(node, a) for a in node}
    fields.update(overrides)
    return type(node)(**fields)


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


_DML_STATEMENTS = (InsertStmt, UpdateStmt, DeleteStmt)

_DML_VERBS = {
    InsertStmt: ("INSERT", "挿入した"),
    UpdateStmt: ("UPDATE", "更新した"),
    DeleteStmt: ("DELETE", "削除した"),
}


def _unique(names: Sequence[str | None]) -> list[str]:
    result: list[str] = []
    for name in names:
        if name and name not in result:
            result.append(name)
    return result


def _target_table(relation: RangeVar) -> RangeVar:
    """DMLの対象テーブルから、エイリアスだけを外したRangeVarを作る
    (実行後のテーブル全体をSELECTするため)。
    """
    return RangeVar(
        schemaname=relation.schemaname,
        relname=relation.relname,
        inh=relation.inh,
        relpersistence=relation.relpersistence,
    )


def _target_star(relation: RangeVar) -> ResTarget:
    """RETURNING句に置く「対象テーブル.*」(エイリアスがあればエイリアスで修飾する)"""
    qualifier = relation.alias.aliasname if relation.alias is not None else relation.relname
    return ResTarget(val=ColumnRef(fields=(String(sval=qualifier), A_Star())))


def _after_state_select(relation: RangeVar, primary_key: Sequence[str]) -> SelectStmt:
    """DML実行後の対象テーブル全体を取得するSELECT文。

    主キーがあれば主キー順に並べる。並べないと、UPDATEで書き換えた行が
    (新しい行バージョンとして書き込まれるため)テーブルの末尾に移動して見え、
    「何が変わったか」を実行前と見比べにくくなるため。
    """
    sort_clause = tuple(
        SortBy(
            node=ColumnRef(fields=(String(sval=col),)),
            sortby_dir=SortByDir.SORTBY_DEFAULT,
            sortby_nulls=SortByNulls.SORTBY_NULLS_DEFAULT,
        )
        for col in primary_key
    )
    return SelectStmt(
        targetList=_star_target_list(),
        fromClause=(_target_table(relation),),
        sortClause=sort_clause or None,
    )


class _DefaultDetector(Visitor):
    """VALUESリストにDEFAULTキーワード(SetToDefault)が含まれるかを調べる"""

    def __init__(self) -> None:
        self.found = False

    def visit_SetToDefault(self, ancestors, node):  # noqa: N802 (pglastの命名規則に合わせる)
        self.found = True


def _contains_default(node: Any) -> bool:
    detector = _DefaultDetector()
    detector(node)
    return detector.found


def _build_update_steps(stmt: UpdateStmt) -> list[_StepDef]:
    """UPDATE文の、DMLを実行する前までのステップ(対象テーブル → WHERE → SET)を組み立てる"""
    star = _star_target_list()
    from_items = (stmt.relation, *(stmt.fromClause or ()))
    set_columns = _unique([target.name for target in stmt.targetList])

    steps = [_StepDef(
        label="対象テーブル + FROM" if stmt.fromClause else "対象テーブル",
        description=(
            "更新対象のテーブルとFROM句のテーブルを組み合わせた全行を取得します(結合条件はWHERE句で指定します)"
            if stmt.fromClause else "更新対象のテーブルの全行を取得します"
        ),
        sql=_deparse(SelectStmt(targetList=star, fromClause=from_items)),
        highlight_columns=_referenced_column_names(stmt.fromClause),
    )]

    if stmt.whereClause is not None:
        steps.append(_StepDef(
            label="WHERE",
            description="条件に一致する行(=更新される行)だけに絞り込みます",
            sql=_deparse(SelectStmt(targetList=star, fromClause=from_items, whereClause=stmt.whereClause)),
            highlight_columns=_referenced_column_names(stmt.whereClause),
        ))

    steps.append(_StepDef(
        label="SET",
        description="SET句の値で対象の行を更新します。表示しているのは更新後の値です",
        sql=_deparse(_clone_with(stmt, returningClause=ReturningClause(exprs=(_target_star(stmt.relation),)))),
        highlight_columns=set_columns,
        source="affected",
    ))
    return steps


def _build_delete_steps(stmt: DeleteStmt) -> list[_StepDef]:
    """DELETE文の、DMLを実行する前までのステップ(対象テーブル → WHERE)を組み立てる"""
    star = _star_target_list()
    from_items = (stmt.relation, *(stmt.usingClause or ()))

    steps = [_StepDef(
        label="対象テーブル + USING" if stmt.usingClause else "対象テーブル",
        description=(
            "削除対象のテーブルとUSING句のテーブルを組み合わせた全行を取得します(結合条件はWHERE句で指定します)"
            if stmt.usingClause else "削除対象のテーブルの全行を取得します"
        ),
        sql=_deparse(SelectStmt(targetList=star, fromClause=from_items)),
        highlight_columns=_referenced_column_names(stmt.usingClause),
    )]

    if stmt.whereClause is not None:
        steps.append(_StepDef(
            label="WHERE",
            description="条件に一致する行(=削除される行)だけに絞り込みます",
            sql=_deparse(SelectStmt(targetList=star, fromClause=from_items, whereClause=stmt.whereClause)),
            highlight_columns=_referenced_column_names(stmt.whereClause),
        ))
    return steps


def _build_insert_steps(stmt: InsertStmt) -> list[_StepDef]:
    """INSERT文の、挿入する行 → INSERT(ON CONFLICT) のステップを組み立てる"""
    steps: list[_StepDef] = []
    columns = _unique([col.name for col in stmt.cols or ()])
    source = stmt.selectStmt
    table = stmt.relation.relname

    # VALUES/SELECTが返す「挿入する行」を、挿入先の列名を付けて単独で見せる。
    # DEFAULTキーワードはINSERTの文脈でしか使えず単独実行できないため、
    # その場合はこのステップを省き、次のINSERTステップ(既定値が入った実際の行)で見せる
    if source is not None and not _contains_default(source):
        is_values = bool(source.valuesLists)
        alias = Alias(
            aliasname="new_rows",
            colnames=tuple(String(sval=c) for c in columns) or None,
        )
        steps.append(_StepDef(
            label="挿入する行(VALUES)" if is_values else "挿入する行(SELECT)",
            description=(
                ("VALUES句で指定した、挿入する行です" if is_values else "SELECT文を実行し、挿入する行を求めます")
                + ("(列名は挿入先の列に合わせています)" if columns else "")
            ),
            sql=_deparse(SelectStmt(
                targetList=_star_target_list(),
                fromClause=(RangeSubselect(subquery=source, alias=alias),),
            )),
            highlight_columns=[],
        ))

    on_conflict = stmt.onConflictClause
    description = (
        f"{table}テーブルに行を挿入します。列の指定を省略した列やDEFAULTを指定した列には、"
        "既定値(SERIALの採番など)が入ります(採番はROLLBACKしても戻らないため、実行するたびに番号が進みます)"
    )
    if on_conflict is None:
        label = "INSERT"
        highlight = columns
    else:
        conflict_columns = (
            [elem.name for elem in on_conflict.infer.indexElems or ()]
            if on_conflict.infer is not None else []
        )
        if on_conflict.action == OnConflictAction.ONCONFLICT_UPDATE:
            label = "INSERT ... ON CONFLICT DO UPDATE"
            description += (
                "。一意制約に衝突する行は、挿入する代わりに既存の行をDO UPDATEのSET句で更新します"
                "(表示しているのは挿入または更新された行です)"
            )
            update_columns = [target.name for target in on_conflict.targetList or ()]
            highlight = _unique([*conflict_columns, *update_columns])
        else:
            label = "INSERT ... ON CONFLICT DO NOTHING"
            description += "。一意制約に衝突する行は挿入せずスキップします(表示しているのは実際に挿入された行だけです)"
            highlight = _unique(conflict_columns)

    steps.append(_StepDef(
        label=label,
        description=description,
        sql=_deparse(_clone_with(stmt, returningClause=ReturningClause(exprs=(_target_star(stmt.relation),)))),
        highlight_columns=highlight,
        source="affected",
    ))
    return steps


def _build_dml_steps(
    stmt: InsertStmt | UpdateStmt | DeleteStmt,
    primary_key: Sequence[str],
) -> tuple[list[_StepDef], _DmlExecution]:
    """DML文のステップ一覧と、DMLを1回だけ実行するためのSQLを組み立てる。

    文の種類ごとのステップ(UPDATEなら対象テーブル → WHERE → SET)に続けて、
    RETURNING句があればその出力、最後に実行後のテーブル全体を見せる
    (ステップ0「実行前」と対になる)。
    """
    if isinstance(stmt, UpdateStmt):
        steps = _build_update_steps(stmt)
        after_highlight = _unique([target.name for target in stmt.targetList])
    elif isinstance(stmt, DeleteStmt):
        steps = _build_delete_steps(stmt)
        after_highlight = []
    else:
        steps = _build_insert_steps(stmt)
        after_highlight = []

    verb, past = _DML_VERBS[type(stmt)]
    table = stmt.relation.relname
    after = _after_state_select(stmt.relation, primary_key)

    if stmt.returningClause is not None:
        steps.append(_StepDef(
            label="RETURNING",
            description=f"RETURNING句で指定した値を、{past}行ごとに返します",
            sql=_deparse(stmt),
            highlight_columns=[],
            source="returning",
        ))

    steps.append(_StepDef(
        label="実行後のテーブル",
        description=(
            f"{verb}を実行した後の、{table}テーブル全体の状態です。このアプリではINSERT/UPDATE/DELETEを"
            "トランザクション内で実行し、最後にROLLBACKするため、実際のデータは変更されません"
        ),
        sql=f"{_deparse(_clone_with(stmt, returningClause=None))};\n{_deparse(after)}",
        highlight_columns=after_highlight,
        source="after",
    ))

    returning = ReturningClause(
        options=stmt.returningClause.options if stmt.returningClause is not None else None,
        exprs=(
            _target_star(stmt.relation),
            *(stmt.returningClause.exprs if stmt.returningClause is not None else ()),
        ),
    )
    execution = _DmlExecution(
        dml_sql=_deparse(_clone_with(stmt, returningClause=returning)),
        after_sql=_deparse(after),
    )
    return steps, execution


def _validate_select(stmt: SelectStmt) -> SelectStmt:
    """対応しているSELECT文かどうかを検証する"""
    if stmt.valuesLists:
        raise UnsupportedQueryError("VALUES句のみのSELECT文は未対応です")
    if stmt.lockingClause:
        raise UnsupportedQueryError("FOR UPDATE/FOR SHAREなどのロック句は未対応です")
    if stmt.withClause is not None and any(
        not isinstance(cte.ctequery, SelectStmt) for cte in stmt.withClause.ctes
    ):
        raise UnsupportedQueryError("WITH句の中でのINSERT/UPDATE/DELETEは未対応です")

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


def _validate_dml(stmt: InsertStmt | UpdateStmt | DeleteStmt) -> InsertStmt | UpdateStmt | DeleteStmt:
    """対応しているINSERT/UPDATE/DELETE文かどうかを検証する"""
    if stmt.withClause is not None:
        raise UnsupportedQueryError("INSERT/UPDATE/DELETE文でのWITH句は未対応です")
    if isinstance(stmt, (UpdateStmt, DeleteStmt)) and isinstance(stmt.whereClause, CurrentOfExpr):
        raise UnsupportedQueryError("WHERE CURRENT OF(カーソルを使った更新・削除)は未対応です")
    return stmt


def _parse_statement(sql: str) -> SelectStmt | InsertStmt | UpdateStmt | DeleteStmt:
    """SQLを解析し、対応する文(SELECT/INSERT/UPDATE/DELETE)かどうかを
    検証したうえでASTを返す"""
    try:
        parsed = parse_sql(sql)
    except ParseError as exc:
        raise UnsupportedQueryError(f"SQLの構文解析に失敗しました: {exc}") from exc

    if len(parsed) != 1:
        raise UnsupportedQueryError("1つのSQL文のみ入力してください")

    stmt = parsed[0].stmt
    if isinstance(stmt, SelectStmt):
        return _validate_select(stmt)
    if isinstance(stmt, _DML_STATEMENTS):
        return _validate_dml(stmt)
    if isinstance(stmt, MergeStmt):
        raise UnsupportedQueryError("MERGE文は未対応です")
    raise UnsupportedQueryError("SELECT/INSERT/UPDATE/DELETE文のみ対応しています")


def _plan_steps(
    stmt: SelectStmt | InsertStmt | UpdateStmt | DeleteStmt,
    primary_key: Sequence[str] = (),
) -> tuple[list[_StepDef], _DmlExecution | None]:
    """文の種類に応じてステップ一覧を組み立てる。DML文の場合は、DMLを1回だけ
    実行するためのSQLもあわせて返す(primary_keyは実行後のテーブルの並び順に使う)。
    """
    if isinstance(stmt, _DML_STATEMENTS):
        return _build_dml_steps(stmt, primary_key)
    if stmt.op != SetOperation.SETOP_NONE:
        nodes = _build_setop_step_nodes(stmt)
    else:
        nodes = _build_step_nodes(stmt)
    steps = [_StepDef(label, desc, _deparse(node), highlight) for label, desc, node, highlight in nodes]
    return steps, None


def parse_to_steps(sql: str, primary_key: Sequence[str] = ()) -> list[tuple[str, str, str, list[str]]]:
    """SQLを解析し、(ラベル, 説明, そのステップのSQL文字列, ハイライト列名) の
    リストを返す"""
    steps, _execution = _plan_steps(_parse_statement(sql), primary_key)
    return [(s.label, s.description, s.sql, s.highlight_columns) for s in steps]


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


def _select_sources(stmt: SelectStmt) -> tuple[list[_FromSource], list[str]]:
    """SELECT文のFROM句が参照している要素と、直後のFROM/JOINステップで
    根拠になる列名(JOIN条件の列)を返す。UNION等の場合は左右2つのSELECT文
    それぞれのFROM句をまとめて対象にし、ハイライトは対象外とする(「次の
    ステップ」が句単位の絞り込みではなく、枝ごとの実行そのものになるため)。
    """
    cte_names = _cte_names(stmt)
    if stmt.op != SetOperation.SETOP_NONE:
        sources = [
            source
            for arm in (stmt.larg, stmt.rarg)
            for source in _extract_from_sources(arm.fromClause or (), cte_names)
        ]
        return sources, []
    return (
        _extract_from_sources(stmt.fromClause or (), cte_names),
        _referenced_column_names(stmt.fromClause),
    )


def _dml_sources(stmt: InsertStmt | UpdateStmt | DeleteStmt) -> tuple[list[_FromSource], list[str], Any]:
    """DML文の対象テーブル(先頭)と、FROM/USING句・INSERTのSELECT文が参照する
    要素を返す。あわせて、直後のステップで根拠になる列名と、CTE参照を
    単独実行するためのWITH句(INSERT ... SELECTのSELECT側が持つもの)も返す。
    """
    alias = stmt.relation.alias.aliasname if stmt.relation.alias is not None else None
    target = _FromSource(kind="table", alias=alias, relname=stmt.relation.relname)

    if isinstance(stmt, UpdateStmt):
        return (
            [target, *_extract_from_sources(stmt.fromClause or (), frozenset())],
            _referenced_column_names(stmt.fromClause),
            None,
        )
    if isinstance(stmt, DeleteStmt):
        return (
            [target, *_extract_from_sources(stmt.usingClause or (), frozenset())],
            _referenced_column_names(stmt.usingClause),
            None,
        )

    source = stmt.selectStmt
    if source is None or source.valuesLists:
        return [target], [], None
    # INSERTの次のステップは「挿入する行」の丸ごとの実行なので、ハイライトは対象外
    sources, _highlight = _select_sources(source)
    return [target, *sources], [], source.withClause


def preview_source_tables(sql: str) -> tuple[list[TablePreview], list[str]]:
    """SQLが参照している各要素の、絞り込み・更新前の生データを取得する。

    「実行」ボタンを押す前でも表示できる、実行前の状態(ステップ0)用。
    実テーブルはそのままSELECTし、CTE参照・FROM句内の派生テーブルは、
    単独実行できる形に組み立て直したうえでSELECTする(いずれも「まだ
    絞り込まれていない、その要素の中身」を見せる)。

    SELECT文ではFROM句の要素、INSERT/UPDATE/DELETE文では対象テーブル(先頭)と
    FROM/USING句・INSERTのSELECT文が参照する要素を対象にする。あわせて、
    直後のステップで根拠になる列名(JOIN条件の列)も返す。
    """
    stmt = _parse_statement(sql)
    star = _star_target_list()

    if isinstance(stmt, _DML_STATEMENTS):
        sources, highlight_columns, with_clause = _dml_sources(stmt)
    else:
        sources, highlight_columns = _select_sources(stmt)
        with_clause = stmt.withClause

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
                    withClause=with_clause,
                )
                preview_sql = f"SELECT * FROM ({_deparse(inner)}) AS step_preview LIMIT {MAX_PREVIEW_ROWS}"
                params = ()
            else:
                label, alias = source.alias or "(サブクエリ)", source.alias
                inner = _standalone_arm(source.subquery, with_clause)
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


@dataclass
class _Fetched:
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    error: str | None = None


def _run_preview(conn: psycopg.Connection, step_sql: str) -> _Fetched:
    """ステップのSELECT文を、プレビュー行数を制限して実行する"""
    preview_sql = f"SELECT * FROM ({step_sql}) AS step_preview LIMIT {MAX_PREVIEW_ROWS}"
    try:
        with conn.cursor() as cur:
            cur.execute(preview_sql)
            return _Fetched(
                columns=[c.name for c in cur.description],
                rows=[[_serialize(v) for v in row] for row in cur.fetchall()],
            )
    except psycopg.Error as exc:
        conn.rollback()
        return _Fetched(error=str(exc).strip())


_PRIMARY_KEY_QUERY = """
    SELECT a.attname
    FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = to_regclass(%s) AND i.indisprimary
    ORDER BY array_position(i.indkey::int2[], a.attnum)
"""


def _primary_key_columns(conn: psycopg.Connection, relation: RangeVar) -> list[str]:
    """対象テーブルの主キー列を定義順に返す(主キーがない・テーブルが存在しない場合は空)"""
    parts = (relation.schemaname, relation.relname) if relation.schemaname else (relation.relname,)
    try:
        with conn.cursor() as cur:
            cur.execute(_PRIMARY_KEY_QUERY, (Identifier(*parts).as_string(conn),))
            return [row[0] for row in cur.fetchall()]
    except psycopg.Error:
        conn.rollback()
        return []


def _run_dml(conn: psycopg.Connection, execution: _DmlExecution) -> dict[str, _Fetched]:
    """DML文を読み書き可能なトランザクションで1回だけ実行し、affected/returning/after
    それぞれの結果を切り出して返す。成功・失敗にかかわらず必ずROLLBACKする。
    """
    # 直前のSELECTステップの(読み取り専用の)トランザクションを終えてから切り替える
    conn.rollback()
    conn.read_only = False
    try:
        with conn.cursor() as cur:
            cur.execute(execution.dml_sql)
            dml_columns = [c.name for c in cur.description]
            dml_rows = [[_serialize(v) for v in row] for row in cur.fetchmany(MAX_PREVIEW_ROWS)]
            cur.execute(f"SELECT * FROM ({execution.after_sql}) AS step_preview LIMIT {MAX_PREVIEW_ROWS}")
            after_columns = [c.name for c in cur.description]
            after_rows = [[_serialize(v) for v in row] for row in cur.fetchall()]
    except psycopg.Error as exc:
        failed = _Fetched(error=str(exc).strip())
        return {"affected": failed, "returning": failed, "after": failed}
    finally:
        conn.rollback()
        conn.read_only = True

    # RETURNING句の先頭には「対象テーブル.*」を置いているので、対象テーブルの列数で切り分ける
    n = len(after_columns)
    return {
        "affected": _Fetched(columns=dml_columns[:n], rows=[row[:n] for row in dml_rows]),
        "returning": _Fetched(columns=dml_columns[n:], rows=[row[n:] for row in dml_rows]),
        "after": _Fetched(columns=after_columns, rows=after_rows),
    }


def execute_steps(sql: str) -> list[StepResult]:
    """各ステップのSQLを実際にPostgreSQLへ発行し、結果を添えて返す。

    途中のステップで実行エラーになった場合は、それ以降のステップは
    実行せずエラー内容を添えて打ち切る(以降も同じ原因で失敗するため)。

    DML文の場合、対象行の絞り込みまでのステップはSELECTとして読み取り専用で
    実行し、DML文そのものは結果が最初に必要になった時点で1回だけ実行する。
    """
    stmt = _parse_statement(sql)
    results: list[StepResult] = []

    with get_connection() as conn:
        primary_key = (
            _primary_key_columns(conn, stmt.relation) if isinstance(stmt, _DML_STATEMENTS) else []
        )
        steps, execution = _plan_steps(stmt, primary_key)
        dml_results: dict[str, _Fetched] | None = None

        for step in steps:
            if step.source == "query":
                fetched = _run_preview(conn, step.sql)
            else:
                if dml_results is None:
                    dml_results = _run_dml(conn, execution)
                fetched = dml_results[step.source]

            results.append(StepResult(
                label=step.label, description=step.description, sql=step.sql,
                columns=fetched.columns, rows=fetched.rows,
                row_count=len(fetched.rows) if fetched.rows is not None else None,
                error=fetched.error, highlight_columns=step.highlight_columns,
            ))
            if fetched.error:
                break

    return results
