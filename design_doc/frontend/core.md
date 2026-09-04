# フロントエンド 中核仕様

親: [../README.md](../README.md)
最終更新: 2026-08-31

このドキュメントには、複数の機能から共通して使われるフロントエンドの技術スタック・実装基盤・オープンな設計課題を書く。機能固有の画面仕様は `features/*.md` を参照。

## 役割

バックエンドAPI(`backend/core.md`参照)を呼び出し、SQL入力・実行結果・可視化を画面に表示する。現時点では単一ページアプリで、SQL文法可視化機能([features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md))がそのままアプリ全体を構成している。今後機能が増えた場合は、本ドキュメントの「共通UI基盤」を各機能が共有する想定。

## 技術スタック

- React 18 + TypeScript + Vite
- SQLエディタ: `@uiw/react-codemirror`(`@codemirror/lang-sql` + `@codemirror/theme-one-dark`)
- 標準のfetch APIによるバックエンド呼び出し(追加のデータフェッチライブラリは未導入)
- スタイリング: Tailwind CSS(方針決定済み・導入は未着手。[オープン課題](#オープン課題-カラーとuiライブラリ)参照)
- アニメーション: Framer Motion(`motion`パッケージ)。[features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md) 3.1節のステップ切り替えアニメーション(`components/StepTable.tsx`)に導入済み。既存のCSS keyframesアニメーション(カード登場のフェード、ハイライトのパルス)は未移行(同上[オープン課題](#オープン課題-カラーとuiライブラリ)参照)

## ディレクトリ構成

```
frontend/
├── src/
│   ├── App.tsx                 画面全体・状態管理・ステップ送り(wheel/ボタン)制御
│   ├── components/
│   │   ├── SqlEditor.tsx       CodeMirrorベースのSQL入力欄(`readOnly`で読み取り専用表示も可)
│   │   ├── StepTable.tsx       大きなカード右側の結果テーブル。列ハイライト対応・Framer Motionによるステップ切り替えアニメーション
│   │   └── StepTimeline.tsx    ステップナビゲーションボタン
│   ├── api/client.ts           バックエンドAPI呼び出し
│   └── index.css
├── package.json
└── Dockerfile
```

## 共通UI基盤

### SQLエディタ

`components/SqlEditor.tsx`。`@uiw/react-codemirror` + `@codemirror/lang-sql`(PostgreSQL方言)+ `@codemirror/theme-one-dark`。アプリ全体のダークトーンに合わせてダークテーマを適用している(2026-08-30、ライトテーマのままで非ハイライト部分が読みづらかった問題を修正)。

**方針**: SQLを表示する箇所(メインの入力欄だけでなく、ステップごとのSQLスニペット表示なども含む)は、すべてこの編集可能なコードコンポーネントで統一する。現状ステップごとのSQLスニペットは読み取り専用の `<pre>` タグで表示しているが、将来的に「ステップ単位でSQLを編集し、そのステップ以降を再計算・再可視化する」機能を見据え、**当面は`readOnly`/`editable={false}`で無効化した同じコンポーネントを使う**ことを標準とする(プレーンテキスト表示は避ける)。詳細は [features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md) を参照。

### APIクライアント

`api/client.ts`。バックエンドの各エンドポイントに対応する `fetch` ラッパー関数を定義し、エラー時は `HTTPException` の `detail` をそのまま `Error` として投げる方式に統一している。`VITE_API_BASE_URL` 環境変数でバックエンドのベースURLを切り替える。

### 固定ヘッダーと「スクロール入力によるステップ送り」

現在 `App.tsx` に実装されている共通パターン(2026-08-31、scrollspy方式から変更):

- 画面上部の入力・ナビゲーション領域(SQL入力欄、ステップボタンなど)を `position: sticky; top: 0` で固定する
- 固定領域より下のコンテンツ表示エリアは、**ビューポートの残り全体にちょうど収まる固定高さ**にし、ページ本体(`window`/`body`)・コンテンツエリア自身のどちらもDOMレベルでは一切スクロールさせない([features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md) 3節参照。当初はscrollspy(スクロール位置から現在地を算出する方式)で実装していたが、ページや内部コンテナが物理的にスクロールすることそのものが要件と合わず、複数回の見直しの末にこの方式に落ち着いた)
- マウスホイール/トラックパッドの`wheel`イベントは`preventDefault`し、スクロールとしては処理しない。その代わり、`deltaY`の累積量が閾値を超えたら「1つ進める/戻す」という離散的な操作(`activeIndex`の更新)として扱う。連続したホイール入力で複数ステップ一気に飛ばないよう、短いクールダウンを設ける
- 加えて、専用の上下ボタン(▲▼)でも同じ「1ステップ進める/戻す」操作ができるようにする
- 固定ヘッダー側のステップ番号ボタン(0〜N)をクリックした場合は、該当のインデックスへ直接ジャンプする(スクロール計算は不要、単に状態を更新するだけ)
- 「今どのステップを見ているか」の表示中コンテンツの切り替えは、`AnimatePresence`(Framer Motion)によるフェード+スライドで行う。進む/戻るの向きを判定し、アニメーションの向きをその都度切り替える
- 固定ヘッダー自身の高さ測定には`ResizeObserver`を使うが、そのコールバックで受け取れる`entry.contentRect`はpadding/borderを含まない内容ボックスの高さしか返さない。ヘッダーにはpadding/borderがあるため、コンテンツ表示エリアの高さ計算(`100vh - ヘッダー高さ`)に使う値は、`contentRect`ではなく`el.offsetHeight`(border-box)で測り直す必要がある(でないと数十px分ずれて、下端の要素が`overflow:hidden`で見えなくなる)

この仕組み自体は機能に依存しない一般的なパターンとして書いているが、現状のコードは [features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md) 専用に `App.tsx` 内へ直書きされている。他機能を追加する際、共通化(カスタムフック化など)を検討する。

### アニメーション規約

- コンテンツブロックの初回表示: フェード+わずかな上方向スライド(`opacity`, `translateY`)。複数ブロックが同時に現れる場合は `animation-delay` でごく短い時間差(目安: 80ms間隔)をつけて順に見せる
- 強調表示(ハイライト): 表示直後に一瞬強い色でパルスさせ、通常の強調色に落ち着かせる(CSS `@keyframes` + `animation`)
- ステップの切り替え: 上記[固定ヘッダーと「スクロール入力によるステップ送り」](#固定ヘッダーとスクロール入力によるステップ送り)の通り、DOMのスクロールではなく`AnimatePresence`によるフェード+スライドで表現する(瞬間的な切り替えは避ける)

## オープン課題 カラーとUIライブラリ

このアプリはフォーム系UI(入力欄・モーダル等の汎用部品)よりも、テーブル・図解・アニメーションといった**独自のビジュアライゼーションが大半**を占める。そのため、汎用コンポーネントライブラリ(shadcn/ui, Mantine等)の恩恵は薄いと判断し、「スタイリング」と「アニメーション」を分けてライブラリを決定した。

- [x] SQLエディタのライトテーマ起因の可読性問題の解消(ダークテーマ適用)
- [x] スタイリング・アニメーションのライブラリ方針決定
  - **スタイリング(色・余白・タイポグラフィのトークン化)**: Tailwind CSS
  - **アニメーション**: Framer Motion(`motion`パッケージ)。特に [features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md) 3.1節のステップ切り替えアニメーション(JOINの連結・行/列の絞り込みが**双方向**に再生される)は、手書きCSS keyframesで行・列の同一性を追跡した差分トランジションを組むのは困難なため、`layout`プロパティによる自動FLIPアニメーションと`AnimatePresence`による出入りアニメーションを持つFramer Motionの採用を前提に設計する
- [ ] Tailwind CSSの導入・既存の手書きCSS(`index.css`)からの移行
- [x] Framer Motionの導入・3.1節のステップ切り替えアニメーションへの適用(`components/StepTable.tsx`)
- [ ] 既存のCSS keyframesアニメーション(カード登場のフェード、ハイライトのパルス)のFramer Motionへの移行

### 将来フェーズ向けの可視化ライブラリ候補(未決定)

汎用スタイリング/アニメーションとは別に、図解・チャート系の機能ではさらに専用のライブラリが必要になる見込み。着手時に決定する候補として記録しておく:

- **React Flow**: ノード/エッジ形式の図が得意。[features/02-execution-order-diagram.md](../features/02-execution-order-diagram.md)(実行順序の図解)・[features/03-ast-tree-view.md](../features/03-ast-tree-view.md)(ASTツリー)・[features/05-lock-range-visualization.md](../features/05-lock-range-visualization.md)(ロック範囲)のいずれにも転用しやすい
- **Recharts**: [features/06-index-performance-comparison.md](../features/06-index-performance-comparison.md)(インデックス有無の性能比較)のBefore/Afterの棒グラフ程度であれば十分

## この仕様を使っている機能

- [features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md)
