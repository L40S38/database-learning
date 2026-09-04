# SQL/DB学習アプリ 設計ドキュメント

最終更新: 2026-08-31

## この repo について

SQLの文法・DBの内部動作(トランザクション/ロック/インデックス)を、可視化しながら学べるアプリ・レポジトリ。

- **主な利用者像**: SQL初学者ではなく、実務でSQLを書く人が「この構文どういう意味だっけ？」となった時にすぐ理解し直せるツール。あわせて、DB内部の仕組み(トランザクション/ロック等)を実務者として体系的に理解したい。
- **性質**: ノートというより「学んだことの可視化ツール」。
- **将来像**: 他人にも使ってもらう公開ホスティングを想定(任意のDB/スキーマを扱えるようにする)。ただし現時点(MVP)は同梱のサンプルスキーマのみ対応。

## ドキュメント構成

```
design_doc/
├── README.md          本ドキュメント。全体像・横断TODO
├── frontend/
│   └── core.md         フロントエンドの中核仕様(技術スタック・共通UI基盤・オープン課題)
├── backend/
│   └── core.md         バックエンドの中核仕様(技術スタック・DB実行方針・データモデル・オープン課題)
└── features/            機能ごとの仕様書
    ├── 01-sql-step-visualization.md    SQL文法可視化(データ変化ステップ表示) — MVP実装済み
    ├── 02-execution-order-diagram.md   実行順序の可視化(図解) — 未着手
    ├── 03-ast-tree-view.md             構文ツリー/AST表示 — 未着手
    ├── 04-transaction-lock-stepper.md  トランザクション/ロックのstep-by-step可視化 — 未着手
    ├── 05-lock-range-visualization.md  ロック範囲の可視化 — 未着手
    └── 06-index-performance-comparison.md インデックス有無による性能比較 — 未着手
```

**方針**: `frontend/core.md` と `backend/core.md` には、複数の機能から共通して使われる技術スタック・実装基盤・オープンな設計課題を書く。`features/*.md` には機能固有の仕様(何をするか・API・UI・スコープ)を書き、実装で使っているcore側のセクションにリンクする。機能から見て「この部分の実装はcoreのこの仕組みに乗っている」という対応関係を辿れるようにする。

## 目指す4つの柱

| # | 機能 | ステータス | 仕様書 |
|---|---|---|---|
| 1 | SQLの文法・構造の可視化 | **MVP実装済み**(データ変化ステップ表示)。表示レイアウトの変更を計画中 | [features/01-sql-step-visualization.md](features/01-sql-step-visualization.md) |
| 1' | └ 実行順序の可視化(図解) | 未着手 | [features/02-execution-order-diagram.md](features/02-execution-order-diagram.md) |
| 1'' | └ 構文ツリー/AST表示 | 未着手 | [features/03-ast-tree-view.md](features/03-ast-tree-view.md) |
| 2 | トランザクション・ロックのstep-by-step可視化 | 未着手 | [features/04-transaction-lock-stepper.md](features/04-transaction-lock-stepper.md) |
| 3 | ロック範囲の可視化 | 未着手 | [features/05-lock-range-visualization.md](features/05-lock-range-visualization.md) |
| 4 | インデックス有無による性能比較 | 未着手 | [features/06-index-performance-comparison.md](features/06-index-performance-comparison.md) |

## システム構成

```
frontend (React + TypeScript, Vite)
    │  fetch (JSON)
    ▼
backend (Python + FastAPI)
    │  psycopg (SQL実行)
    ▼
db (PostgreSQL, サンプルスキーマ)
```

独自の実行エンジンを実装するのではなく、本物のPostgreSQLに実際にSQLを投げて結果を取得する方式を基本方針とする。詳細は [backend/core.md](backend/core.md) を参照。

## 起動方法

```
docker compose up -d --build   # 起動
docker compose down            # 停止
```

ポートは環境の別プロジェクトとの衝突を避けるため、一般的なデフォルト(5432/8000/5173)から変更済み。

| サービス | ポート |
|---|---|
| frontend | 5174 |
| backend | 8001 |
| db (PostgreSQL) | 5433 |

## 横断TODO

機能に閉じない、複数機能・アプリ全体にまたがる課題。詳細は各リンク先を参照。

- [ ] [features/01-sql-step-visualization.md](features/01-sql-step-visualization.md) — テーブル結果表示エリアの左右分割スクロールテリング化(仕様決定済み・実装待ち)
- [ ] [frontend/core.md](frontend/core.md#オープン課題-カラーとuiライブラリ) — カラー/UIライブラリの方向性決定
- [ ] [backend/core.md](backend/core.md#オープン課題-テーブル設計のマスタ化) — `prefectures` / `categories` マスタテーブル化の要否判断
- [ ] [backend/core.md](backend/core.md#オープン課題-公開ホスティングに向けたサンドボックス化) — 公開ホスティング時のサンドボックス設計(セッション分離・危険文禁止・レート制限)
- [ ] ユーザー自身のDB/スキーマ接続対応
- [ ] [features/01-sql-step-visualization.md](features/01-sql-step-visualization.md#5-sql構文カバレッジ現状とロードマップ) — SQL構文カバレッジの拡大(CTE・サブクエリ・ウィンドウ関数・UNION系・DML・DDL、フェーズ分けしたロードマップ)
