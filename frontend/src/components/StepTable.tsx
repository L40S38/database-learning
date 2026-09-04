import { AnimatePresence, motion, type Variants } from "motion/react";
import { useEffect, useRef } from "react";
import type { TablePreview } from "../api/client";

/**
 * 右カラムに表示する「今アクティブなステップ」の中身。
 * ステップ0(実行前)だけは元テーブルが複数あり得るため "source"、
 * それ以外(FROM/JOIN 以降の各ステップ)は単一の結果テーブルなので "result" として区別する。
 * この2つの間の遷移(複数テーブル→1テーブル)は、行・列単位でのFLIPが成立しない
 * 別物のビューなので、後述のとおり全体をクロスフェード/収束させる形で表現する。
 */
export type ActiveStepView =
  | {
      kind: "source";
      tables: TablePreview[];
      highlightColumns: string[];
    }
  | {
      kind: "result";
      label: string;
      columns: string[] | null;
      rows: unknown[][] | null;
      row_count: number | null;
      error: string | null;
      highlightColumns: string[];
    };

interface Props {
  view: ActiveStepView;
  /** 直前のアクティブステップから見て、スクロールが進んでいる(1)か戻っている(-1)か */
  direction: 1 | -1;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  return String(value);
}

// 行には安定したID(主キー)がないため、直前ステップと共通の列の値を行の
// 同一性の代用にする。同じ値の行が複数ある場合は出現順で区別する。
function computeRowKeys(columns: string[], rows: unknown[][], keyColumns: string[]): string[] {
  const seen = new Map<string, number>();
  return rows.map((row) => {
    const base =
      keyColumns.length > 0
        ? keyColumns.map((c) => JSON.stringify(row[columns.indexOf(c)])).join("|")
        : JSON.stringify(row);
    const occurrence = seen.get(base) ?? 0;
    seen.set(base, occurrence + 1);
    return `${base}#${occurrence}`;
  });
}

// FROM/JOINのSELECT *プレビューでは、結合元テーブル同士で同名の列
// (例: customer_id)が重複して並ぶことがあるため、列名だけでは
// Reactのkeyやハイライト判定の同一性として不十分。出現順で区別する。
function computeColumnKeys(columns: string[]): string[] {
  const seen = new Map<string, number>();
  return columns.map((col) => {
    const occurrence = seen.get(col) ?? 0;
    seen.set(col, occurrence + 1);
    return `${col}#${occurrence}`;
  });
}

// 直前レンダー時点の列一覧を覚えておき、「今回と直前の両方に存在する列」だけを
// 行キーの計算に使う。SELECTで列が絞り込まれても、絞り込み前後で行の対応が
// 取れるようにするため。
function useRowKeyColumns(columns: string[]): string[] {
  const prevColumnsRef = useRef<string[] | null>(null);
  const prevColumns = prevColumnsRef.current;
  const keyColumns = prevColumns ? columns.filter((c) => prevColumns.includes(c)) : columns;
  useEffect(() => {
    prevColumnsRef.current = columns;
  }, [columns]);
  return keyColumns;
}

const viewVariants: Variants = {
  enter: (direction: 1 | -1) => ({ opacity: 0, scale: 0.94, y: direction === 1 ? 16 : -16 }),
  center: { opacity: 1, scale: 1, y: 0 },
  exit: (direction: 1 | -1) => ({ opacity: 0, scale: 0.94, y: direction === 1 ? -16 : 16 }),
};

// FROM/JOINへ向かう(1)ときは元テーブルが中心に寄って消え、
// 実行前へ戻る(-1)ときは中心から離れるように広がって現れる
const sourceTableVariants: Variants = {
  enter: (direction: 1 | -1) => ({ opacity: 0, scale: 0.85, x: direction === 1 ? -20 : 20 }),
  center: { opacity: 1, scale: 1, x: 0 },
  exit: (direction: 1 | -1) => ({ opacity: 0, scale: 0.85, x: direction === 1 ? 20 : -20 }),
};

const rowVariants: Variants = {
  enter: (direction: 1 | -1) => ({ opacity: 0, y: direction === 1 ? -6 : 6 }),
  center: { opacity: 1, y: 0 },
  exit: (direction: 1 | -1) => ({ opacity: 0, y: direction === 1 ? 6 : -6 }),
};

interface SingleTableProps {
  columns: string[] | null;
  rows: unknown[][] | null;
  row_count: number | null;
  error: string | null;
  highlightColumns: string[];
  direction: 1 | -1;
}

// 単一テーブルの表示。列の増減(SELECTでの絞り込み)・行の増減(WHERE/HAVINGでの
// 絞り込み)を、AnimatePresence + layout による自動FLIPで表現する。
// 現在のpropsだけを描画すれば、消えた行/列はAnimatePresenceが直前の見た目を
// 保持したまま退場アニメーションを再生してくれるため、双方向(スクロールを
// 戻したときは逆に「増える」)でも同じロジックで自然に成立する。
function SingleTable({ columns, rows, row_count, error, highlightColumns, direction }: SingleTableProps) {
  const keyColumns = useRowKeyColumns(columns ?? []);

  if (error) {
    return (
      <div className="step-error">
        <strong>実行エラー:</strong> {error}
      </div>
    );
  }

  if (!columns || !rows) {
    return <p className="step-empty">まだ結果がありません</p>;
  }

  if (rows.length === 0) {
    return <p className="step-empty">結果は0件でした</p>;
  }

  const highlightSet = new Set(highlightColumns);
  const rowKeys = computeRowKeys(columns, rows, keyColumns);
  const columnKeys = computeColumnKeys(columns);

  return (
    <div className="step-table-wrapper">
      <table className="step-table">
        <thead>
          <tr>
            <AnimatePresence initial={false} mode="popLayout">
              {columns.map((col, colIndex) => (
                <motion.th
                  key={columnKeys[colIndex]}
                  layout
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.25 }}
                  className={highlightSet.has(col) ? "col-highlight" : undefined}
                >
                  {col}
                </motion.th>
              ))}
            </AnimatePresence>
          </tr>
        </thead>
        <tbody>
          <AnimatePresence initial={false} custom={direction} mode="popLayout">
            {rows.map((row, rowIndex) => (
              <motion.tr
                key={rowKeys[rowIndex]}
                layout
                custom={direction}
                variants={rowVariants}
                initial="enter"
                animate="center"
                exit="exit"
                transition={{ duration: 0.3, ease: "easeOut" }}
              >
                <AnimatePresence initial={false} mode="popLayout">
                  {row.map((cell, cellIndex) => (
                    <motion.td
                      key={columnKeys[cellIndex]}
                      layout
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: 0.25 }}
                      className={highlightSet.has(columns[cellIndex]) ? "col-highlight" : undefined}
                    >
                      {formatCell(cell)}
                    </motion.td>
                  ))}
                </AnimatePresence>
              </motion.tr>
            ))}
          </AnimatePresence>
        </tbody>
      </table>
      <p className="step-row-count">{row_count} 行</p>
    </div>
  );
}

export default function StepTable({ view, direction }: Props) {
  return (
    <AnimatePresence mode="sync" custom={direction} initial={false}>
      {view.kind === "source" ? (
        <motion.div
          key="source-view"
          layout
          custom={direction}
          variants={viewVariants}
          initial="enter"
          animate="center"
          exit="exit"
          transition={{ duration: 0.35, ease: "easeOut" }}
          className="result-view"
        >
          {view.tables.length === 0 && <p className="step-empty">SQLを入力すると表示されます</p>}
          <div className="source-table-grid">
            <AnimatePresence initial={false} custom={direction} mode="popLayout">
              {view.tables.map((t, i) => (
                <motion.div
                  key={`${t.table}-${t.alias ?? ""}-${i}`}
                  layout
                  custom={direction}
                  variants={sourceTableVariants}
                  initial="enter"
                  animate="center"
                  exit="exit"
                  transition={{ duration: 0.35, ease: "easeOut" }}
                  className="source-table-block"
                >
                  <h4>
                    {t.table}
                    {t.alias && <span className="source-table-alias"> (エイリアス: {t.alias})</span>}
                  </h4>
                  <SingleTable
                    columns={t.columns}
                    rows={t.rows}
                    row_count={t.row_count}
                    error={t.error}
                    highlightColumns={view.highlightColumns}
                    direction={direction}
                  />
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        </motion.div>
      ) : (
        <motion.div
          key="result-view"
          layout
          custom={direction}
          variants={viewVariants}
          initial="enter"
          animate="center"
          exit="exit"
          transition={{ duration: 0.35, ease: "easeOut" }}
          className="result-view"
        >
          <SingleTable
            columns={view.columns}
            rows={view.rows}
            row_count={view.row_count}
            error={view.error}
            highlightColumns={view.highlightColumns}
            direction={direction}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
