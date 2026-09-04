# Feature: ロック範囲の可視化

親: [../README.md](../README.md)
ステータス: 未着手
最終更新: 2026-08-31

## 関連コア仕様

- Backend: [DB接続と実行方針](../backend/core.md#db接続と実行方針)
- Frontend: [将来フェーズ向けの可視化ライブラリ候補](../frontend/core.md#将来フェーズ向けの可視化ライブラリ候補未決定)(React Flowが候補)
- 関連機能: [04-transaction-lock-stepper.md](04-transaction-lock-stepper.md)(セッション管理基盤に依存)

## 実装状況

**MVP(現状)で実装済み**
- (なし)

**拡張・実装していく範囲(未実装)**
- [ ] `pg_locks` の照会ロジック
- [ ] ロック対象(行/ページ/テーブル)の可視化

## 内容

特定のSQL実行時に、どの行・テーブル・範囲がロックされるかを図で示す。

## 必要な作業

- [ ] `pg_locks` の照会、ロック対象(行/ページ/テーブル)の可視化ロジック
- [ ] [04-transaction-lock-stepper.md](04-transaction-lock-stepper.md)のセッション管理基盤に依存

## 規模感

M([04-transaction-lock-stepper.md](04-transaction-lock-stepper.md)の基盤があれば追加コストは中程度)
