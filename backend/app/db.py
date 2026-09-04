import os

import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://sqlviz:sqlviz@localhost:5432/sqlviz",
)

# 学習用ツールでの安全側デフォルト。将来の公開ホスティング時に
# 本格的なサンドボックス化(セッション分離ロール/コンテナ等)へ差し替える前提の
# 最低限のガード。
STATEMENT_TIMEOUT_MS = 5000


def get_connection() -> psycopg.Connection:
    """読み取り専用・タイムアウト付きの新規接続を返す"""
    conn = psycopg.connect(DATABASE_URL)
    conn.read_only = True
    conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn
