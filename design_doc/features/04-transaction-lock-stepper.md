# Feature: トランザクション・ロックのstep-by-step可視化

親: [../README.md](../README.md)
ステータス: 未着手
最終更新: 2026-11-03

## 関連コア仕様

- Backend: [DB接続と実行方針](../backend/core.md#db接続と実行方針) / [オープン課題 公開ホスティングに向けたサンドボックス化](../backend/core.md#オープン課題-公開ホスティングに向けたサンドボックス化)(前提条件、下記参照)
- Frontend: [固定ヘッダーとスクロール連動](../frontend/core.md#固定ヘッダーとスクロール連動)
- 関連機能: [01-sql-step-visualization.md](01-sql-step-visualization.md) の Phase B(前提条件、下記参照)

## 実装状況

**MVP(現状)で実装済み**
- (なし)

**拡張・実装していく範囲(未実装)**
- [ ] 複数疑似セッションを制御するオーケストレーション層
- [ ] `pg_locks` / `pg_stat_activity` のポーリング・通知の仕組み
- [ ] 複数セッションを並べたタイムラインUI

## 内容

複数セッション(トランザクション)が絡む処理の流れを1ステップずつ可視化する。

## 必要な作業

- [ ] バックエンド: 複数の疑似セッション(DBコネクション)を制御し、任意のタイミングでSQLを1文ずつ実行できるオーケストレーション層が必要([01-sql-step-visualization.md](01-sql-step-visualization.md)の「1回のSELECTを分解する」設計とは別軸の機能)
- [ ] `pg_locks` / `pg_stat_activity` の定期ポーリングまたは通知の仕組み
- [ ] フロント: 複数セッションを並べたタイムラインUI

## 規模感

L(新規のセッション管理・状態管理が必要。[01-sql-step-visualization.md](01-sql-step-visualization.md)の資産の一部(実行基盤・SQL解析)は再利用可能)

## 前提

- **[01-sql-step-visualization.md](01-sql-step-visualization.md) の Phase B(INSERT/UPDATE/DELETE対応)は最低限必須**。ロック・トランザクションの挙動は書き込み(INSERT/UPDATE/DELETE)を伴って初めて意味のある可視化になる(例: 「セッションAがある行をUPDATEし、セッションBが同じ行をUPDATEしようとしてブロックされる」)。SELECT文の分解・実行しかできない状態のままでは、この機能で見せたいシナリオを1つも構成できない。着手前に01のPhase Bを完了させておくこと。→ **2026-11-03にPhase B実装済み(前提クリア)**。ただし01のDML実行は「1リクエスト内で実行して即ROLLBACK」する方式であり、複数セッションにまたがってトランザクションを開いたままにする仕組みはこの機能で新たに作る必要がある。
- 公開ホスティングを見据えるなら、[backend/core.md のサンドボックス化](../backend/core.md#オープン課題-公開ホスティングに向けたサンドボックス化)(セッション分離)を先に固める方が手戻りが少ない。
