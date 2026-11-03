# バックエンド 中核仕様

親: [../README.md](../README.md)
最終更新: 2026-11-03

このドキュメントには、複数の機能から共通して使われるバックエンドの技術スタック・実装基盤・データモデル・オープンな設計課題を書く。機能固有のAPI仕様は `features/*.md` を参照。

## 役割

SQLの解析・分解・実行を受け持ち、フロントエンドへJSONで結果を返すAPIサーバー。独自の実行エンジンを実装するのではなく、本物のPostgreSQLに実際にSQLを投げて結果を取得する方式を基本方針とする(EXPLAINやpg_locksなど実際の挙動をそのまま見せられる正確性を優先)。

## 技術スタック

- Python + FastAPI
- SQL解析・句分解・部分SQL再構築: `pglast`(libpg_query の Python バインディング。本物のPostgreSQLパーサーそのもの)
- AST内の列参照抽出: `pglast.visitors.Visitor`
- DB接続・実行: `psycopg`(v3)
- DB: PostgreSQL 18(Docker)

## ディレクトリ構成

```
backend/
├── app/
│   ├── main.py         FastAPIエントリポイント・エンドポイント定義
│   ├── sql_steps.py     SQL解析・句分解・列ハイライト抽出・実行ロジック
│   └── db.py            DB接続(psycopg)
├── tests/
│   └── test_sql_steps.py
├── requirements.txt
└── Dockerfile
```

## DB接続と実行方針

`app/db.py` の `get_connection()` が新規接続を1件返す(MVP規模のためコネクションプールは未導入)。

### 実行時の安全策

任意のSQLをそのまま実行する設計上、以下の安全策を最低限のデフォルトとして組み込んでいる(公開ホスティングを見据えた本格的な対策は未実装。[オープン課題 公開ホスティングに向けたサンドボックス化](#オープン課題-公開ホスティングに向けたサンドボックス化)を参照):

- [x] 接続を `read_only = True` にし、書き込みを伴う操作を防ぐ
- [x] INSERT/UPDATE/DELETE文([features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md)5節Phase B)だけは、その文を実行する間に限って読み書き可能なトランザクションに切り替え、**成功・失敗にかかわらず必ずROLLBACKしてから読み取り専用に戻す**(`sql_steps.py` の `_run_dml`)。実際のデータは変更されない。ただしシーケンス(SERIALの採番)のようにROLLBACKしても戻らない副作用は残る(2026-11-03追加)
- [x] `statement_timeout` (既定5000ms)で長時間実行を防ぐ。接続オプション(`options="-c statement_timeout=..."`)としてセッション開始時に設定する(2026-11-03修正。以前はトランザクション内の`SET`で設定していたため、エラー時のROLLBACKで設定ごと取り消され、同じ接続での以降の実行がタイムアウトなしになっていた)
- [x] 実行結果はプレビュー用に `SELECT * FROM (<対象SQL>) LIMIT 200` で行数を制限して取得する
- [x] ステップ0(実行前テーブル)のプレビューSQLは、テーブル名を生文字列で埋め込まず `psycopg.sql.Identifier` で安全にクォートする(2026-09-04、レビュー指摘を受けて修正。以前は `f'SELECT * FROM "{relname}" ...'` のような未エスケープの埋め込みだった)
- [x] `FOR UPDATE`/`FOR SHARE`等のロック句は、現状のステップ分解モデルが評価順序を正しく表現できないため明示的に未対応エラーとする(2026-09-04追加。以前はパーサーが拒否せず、黙って句が失われた状態で実行されてしまっていた)。インラインの`OVER (...)`によるウィンドウ関数は、専用の「WINDOW」ステップを追加する形で2026-09-05に対応した([features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md)5節参照)。名前付き`WINDOW`句(`WINDOW w AS (...)`)は参照解決が別途必要なため引き続き明示的に未対応エラーとする

## データモデル サンプルスキーマ

`db/seed.sql` に定義。簡易ECサイトを模した4テーブル構成。

- `customers(customer_id, name, prefecture, created_at)`
- `products(product_id, name, category, price)`
- `orders(order_id, customer_id, order_date, status)`
- `order_items(order_item_id, order_id, product_id, quantity, unit_price)`

## オープン課題 テーブル設計のマスタ化

`customers.prefecture` / `products.category` は現状フラットな文字列列。第3正規形の定義上は違反していないが、表記ゆれ防止・将来の属性拡張を見据えると `prefectures` / `categories` マスタテーブルを切り出す価値はある(このアプリ自体が正規化を学ぶ教材でもあるため)。**今回は見送り**と合意済み。着手する場合は、サンプルクエリ・シードデータ・バックエンドテストへの影響があるため、別途スコープを切って対応する。

- [ ] `prefectures` マスタテーブルの切り出し
- [ ] `categories` マスタテーブルの切り出し

## オープン課題 公開ホスティングに向けたサンドボックス化

任意のSQLを本物のPostgreSQLに実行させる設計上、他人に公開する段階では以下が必要(MVPでは未実装、設計だけ考慮):

- [x] 実行時のタイムアウト設定(`statement_timeout`)
- [x] 接続の読み取り専用化(`read_only`)。DML文だけは、必ずROLLBACKするトランザクション内で実行する(上記「実行時の安全策」参照)。現状のDBロール(`sqlviz`)はスーパーユーザーのため、公開時は下記の制限付きロールが必要
- [ ] セッションごとに分離されたスキーマ、または使い捨てコンテナ
- [ ] 実行専用の制限付きDBロール(DROP/ALTER等の危険な文の禁止)
- [ ] レート制限
- [ ] ユーザー自身のDB/スキーマを扱う機能(接続情報の入力・検証・サンドボックス実行)

[features/04-transaction-lock-stepper.md](../features/04-transaction-lock-stepper.md)・[features/05-lock-range-visualization.md](../features/05-lock-range-visualization.md)・[features/06-index-performance-comparison.md](../features/06-index-performance-comparison.md) は、いずれもこのサンドボックス化を先に固めておいた方が手戻りが少ない。

## この仕様を使っている機能

- [features/01-sql-step-visualization.md](../features/01-sql-step-visualization.md)
