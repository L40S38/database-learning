import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, type Variants } from "motion/react";
import { fetchQuerySteps, fetchSourceTables, type StepResult, type TablePreview } from "./api/client";
import SqlEditor from "./components/SqlEditor";
import StepTable, { type ActiveStepView } from "./components/StepTable";
import StepTimeline from "./components/StepTimeline";

const SAMPLE_QUERIES: { title: string; sql: string }[] = [
  {
    title: "シンプルなJOIN",
    sql: "SELECT o.order_id, c.name, o.order_date\nFROM orders o\nJOIN customers c ON o.customer_id = c.customer_id\nORDER BY o.order_date;",
  },
  {
    title: "GROUP BY + HAVING",
    sql: "SELECT c.prefecture, COUNT(*) AS order_count\nFROM orders o\nJOIN customers c ON o.customer_id = c.customer_id\nWHERE o.status = '完了'\nGROUP BY c.prefecture\nHAVING COUNT(*) > 1\nORDER BY order_count DESC;",
  },
  {
    title: "CTE(WITH句)",
    sql: "WITH completed_orders AS (\n  SELECT customer_id, order_date\n  FROM orders\n  WHERE status = '完了'\n)\nSELECT c.name, co.order_date\nFROM completed_orders co\nJOIN customers c ON co.customer_id = c.customer_id\nORDER BY co.order_date;",
  },
  {
    title: "サブクエリ",
    sql: "SELECT\n  c.name,\n  (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.customer_id) AS order_count\nFROM customers c\nWHERE c.customer_id IN (SELECT customer_id FROM orders WHERE status = '完了')\nORDER BY order_count DESC;",
  },
  {
    title: "ウィンドウ関数",
    sql: "SELECT\n  customer_id,\n  order_date,\n  RANK() OVER (PARTITION BY customer_id ORDER BY order_date) AS order_rank\nFROM orders\nWHERE status = '完了'\nORDER BY customer_id, order_rank;",
  },
  {
    title: "UNION",
    sql: "SELECT name, prefecture FROM customers WHERE prefecture = '東京都'\nUNION\nSELECT name, prefecture FROM customers WHERE prefecture = '大阪府'\nORDER BY name;",
  },
];

const SOURCE_TABLES_DEBOUNCE_MS = 500;
// マウスホイールの累積量がこの値(px)を超えたら1ステップ進める/戻す
const WHEEL_STEP_THRESHOLD = 60;
// 1回のステップ切り替え後、次の切り替えを受け付けるまでのクールダウン(ms)。
// トラックパッド等の連続したwheelイベントで一気に何ステップも飛ばないようにする
const WHEEL_COOLDOWN_MS = 450;

const cardVariants: Variants = {
  enter: (direction: 1 | -1) => ({ opacity: 0, y: direction === 1 ? 32 : -32 }),
  center: { opacity: 1, y: 0 },
  exit: (direction: 1 | -1) => ({ opacity: 0, y: direction === 1 ? -32 : 32 }),
};

export default function App() {
  const [sql, setSql] = useState(SAMPLE_QUERIES[0].sql);
  const [sourceTables, setSourceTables] = useState<TablePreview[]>([]);
  const [sourceHighlight, setSourceHighlight] = useState<string[]>([]);
  const [steps, setSteps] = useState<StepResult[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [headerHeight, setHeaderHeight] = useState(0);

  const headerRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);

  // 固定ヘッダーの高さ(エラー表示の有無などで変わる)を都度測っておき、
  // ステップ表示エリア(大きなカード1枚分)の高さをビューポートの残りにぴったり
  // 合わせる。ページはスクロールさせず、カードそのものが入れ替わることで
  // ステップを切り替える。
  useEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    // ResizeObserverのcontentRectはpadding/borderを含まないため、
    // ヘッダーの実際の表示高さ(offsetHeight = border-box)を都度読み直す
    const observer = new ResizeObserver(() => {
      setHeaderHeight(el.offsetHeight);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const stageHeight = `calc(100vh - ${headerHeight}px - 24px)`;

  // 直前のactiveIndexとの比較で、ステップが進んでいる(1)/戻っている(-1)かを
  // 判定し、カード・右側テーブルのアニメーションの向きに使う
  const prevActiveIndexRef = useRef(0);
  const directionRef = useRef<1 | -1>(1);
  if (activeIndex !== prevActiveIndexRef.current) {
    directionRef.current = activeIndex > prevActiveIndexRef.current ? 1 : -1;
    prevActiveIndexRef.current = activeIndex;
  }
  const direction = directionRef.current;

  // 「実行」を押す前でも、FROM句が指す元テーブルの中身をステップ0として
  // 自動的に取得しておく(入力途中の不完全なSQLでは静かに無視する)。
  useEffect(() => {
    let cancelled = false;
    const handle = setTimeout(() => {
      fetchSourceTables(sql)
        .then(({ tables, highlightColumns }) => {
          // sqlがさらに変わった後にレスポンスが届いた場合、古い結果で
          // 新しい入力の表示を上書きしないようにする
          if (cancelled) return;
          setSourceTables(tables);
          setSourceHighlight(highlightColumns);
        })
        .catch(() => {
          if (cancelled) return;
          // 入力中の不完全なSQLは想定内なのでエラー表示はしないが、
          // 直前に表示していた無関係なテーブルが残り続けると誤解を招くため、
          // 現在のSQLに対する結果としてはクリアする
          setSourceTables([]);
          setSourceHighlight([]);
        });
    }, SOURCE_TABLES_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [sql]);

  const timelineItems = [
    { label: "実行前", hasError: sourceTables.some((t) => !!t.error) },
    ...steps.map((s) => ({ label: s.label, hasError: !!s.error })),
  ];

  // 現在のステップの内容。ステップ0は元テーブルが複数あり得るため "source"、
  // それ以外は単一の結果テーブルなので "result" として渡す。
  const activeStep = steps[activeIndex - 1];
  const activeView: ActiveStepView =
    activeIndex === 0 || !activeStep
      ? { kind: "source", tables: sourceTables, highlightColumns: sourceHighlight }
      : {
          kind: "result",
          label: activeStep.label,
          columns: activeStep.columns,
          rows: activeStep.rows,
          row_count: activeStep.row_count,
          error: activeStep.error,
          highlightColumns: activeStep.highlight_columns,
        };
  const activeLabel = timelineItems[activeIndex]?.label ?? timelineItems[0].label;

  function goToStep(index: number) {
    setActiveIndex(Math.max(0, Math.min(timelineItems.length - 1, index)));
  }

  // マウスホイールでの上下操作を、ページ・カードのスクロールではなく
  // 「1ステップ進める/戻す」という離散的な操作として扱う
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    let accumulated = 0;
    let cooling = false;

    function handleWheel(e: WheelEvent) {
      e.preventDefault();
      if (cooling) return;
      accumulated += e.deltaY;
      if (Math.abs(accumulated) < WHEEL_STEP_THRESHOLD) return;
      const stepDirection = accumulated > 0 ? 1 : -1;
      accumulated = 0;
      cooling = true;
      setActiveIndex((prev) => Math.max(0, Math.min(timelineItems.length - 1, prev + stepDirection)));
      setTimeout(() => {
        cooling = false;
      }, WHEEL_COOLDOWN_MS);
    }

    el.addEventListener("wheel", handleWheel, { passive: false });
    return () => el.removeEventListener("wheel", handleWheel);
  }, [timelineItems.length]);

  async function handleRun() {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchQuerySteps(sql);
      setSteps(result);
      setActiveIndex(0);
    } catch (err) {
      setSteps([]);
      setError(err instanceof Error ? err.message : "不明なエラーが発生しました");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app">
      <div className="sticky-header" ref={headerRef}>
        <header className="app-header">
          <h1>SQL可視化学習アプリ</h1>
          <p>SQLを実行順序ごとに分解し、各ステップでデータがどう変化するかを可視化します</p>
        </header>

        <section className="sample-queries">
          {SAMPLE_QUERIES.map((sample) => (
            <button key={sample.title} type="button" onClick={() => setSql(sample.sql)}>
              {sample.title}
            </button>
          ))}
        </section>

        <section className="editor-section">
          <SqlEditor value={sql} onChange={setSql} />
          <button type="button" className="run-button" onClick={handleRun} disabled={loading}>
            {loading ? "実行中..." : "実行"}
          </button>
        </section>

        {error && <div className="global-error">{error}</div>}

        <StepTimeline items={timelineItems} activeIndex={activeIndex} onSelect={goToStep} />
      </div>

      <div className="step-stage" ref={stageRef} style={{ height: stageHeight }}>
        <span className="visually-hidden" role="status" aria-live="polite">
          {activeLabel}に切り替わりました
        </span>
        <AnimatePresence mode="wait" custom={direction} initial={false}>
          {activeIndex === 0 ? (
            <motion.div
              key="card-0"
              custom={direction}
              variants={cardVariants}
              initial="enter"
              animate="center"
              exit="exit"
              transition={{ duration: 0.35, ease: "easeOut" }}
              className="step-big-card"
            >
              <div className="step-big-card-left">
                <h2>0. 実行前(元テーブル)</h2>
                <p className="step-description">
                  FROM句が参照しているテーブルの、絞り込み・結合される前の中身です。「実行」を押すと、この状態からどう変化するかを1番以降で確認できます。
                </p>
                {sourceTables.length === 0 && <p className="step-empty">SQLを入力すると表示されます</p>}
                {sourceTables.length > 0 && (
                  <ul className="source-table-name-list">
                    {sourceTables.map((t, i) => (
                      <li key={`${t.table}-${t.alias}-${i}`}>
                        {t.table}
                        {t.alias && <span className="source-table-alias"> (エイリアス: {t.alias})</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="step-big-card-right">
                <h3 className="step-result-title">{activeLabel}</h3>
                <StepTable view={activeView} direction={direction} />
              </div>
            </motion.div>
          ) : activeStep ? (
            <motion.div
              key={`card-${activeIndex}`}
              custom={direction}
              variants={cardVariants}
              initial="enter"
              animate="center"
              exit="exit"
              transition={{ duration: 0.35, ease: "easeOut" }}
              className="step-big-card"
            >
              <div className="step-big-card-left">
                <h2>
                  {activeIndex}. {activeStep.label}
                </h2>
                <p className="step-description">{activeStep.description}</p>
                <div className="step-sql-editor">
                  <SqlEditor value={activeStep.sql} readOnly height="auto" />
                </div>
              </div>
              <div className="step-big-card-right">
                <h3 className="step-result-title">{activeLabel}</h3>
                <StepTable view={activeView} direction={direction} />
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>

        <div className="step-nav-buttons">
          <button
            type="button"
            className="step-nav-button"
            onClick={() => goToStep(activeIndex - 1)}
            disabled={activeIndex === 0}
            aria-label="前のステップ"
          >
            ▲
          </button>
          <button
            type="button"
            className="step-nav-button"
            onClick={() => goToStep(activeIndex + 1)}
            disabled={activeIndex === timelineItems.length - 1}
            aria-label="次のステップ"
          >
            ▼
          </button>
        </div>
      </div>
    </div>
  );
}
