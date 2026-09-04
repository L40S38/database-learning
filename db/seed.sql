-- サンプルスキーマ: 簡易ECサイト（顧客・商品・注文・注文明細）
-- SQL可視化のデモ用データ。JOIN/GROUP BY/HAVING/ORDER BYなどが試しやすい規模に絞っている。

CREATE TABLE customers (
    customer_id   SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    prefecture    TEXT NOT NULL,
    created_at    DATE NOT NULL
);

CREATE TABLE products (
    product_id    SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    price         INTEGER NOT NULL
);

CREATE TABLE orders (
    order_id      SERIAL PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date    DATE NOT NULL,
    status        TEXT NOT NULL
);

CREATE TABLE order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    INTEGER NOT NULL
);

INSERT INTO customers (name, prefecture, created_at) VALUES
    ('田中太郎', '東京都', '2024-01-10'),
    ('鈴木花子', '大阪府', '2024-02-15'),
    ('佐藤健',   '東京都', '2024-03-02'),
    ('高橋美咲', '福岡県', '2024-03-20'),
    ('伊藤翔太', '大阪府', '2024-04-05'),
    ('渡辺陽子', '北海道', '2024-05-11');

INSERT INTO products (name, category, price) VALUES
    ('ノートPC',       '電化製品', 98000),
    ('ワイヤレスマウス', '電化製品', 2500),
    ('コーヒー豆',      '食品',    1200),
    ('紅茶セット',      '食品',    1800),
    ('デスクライト',    '家具',    4500),
    ('オフィスチェア',  '家具',    32000);

INSERT INTO orders (customer_id, order_date, status) VALUES
    (1, '2024-04-01', '完了'),
    (1, '2024-05-03', '完了'),
    (2, '2024-04-10', '完了'),
    (3, '2024-04-15', 'キャンセル'),
    (3, '2024-05-20', '完了'),
    (4, '2024-05-01', '完了'),
    (5, '2024-05-25', '発送待ち'),
    (6, '2024-06-01', '完了');

INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 1, 98000),
    (1, 2, 1, 2500),
    (2, 3, 2, 1200),
    (3, 5, 1, 4500),
    (3, 4, 1, 1800),
    (4, 6, 1, 32000),
    (5, 2, 2, 2500),
    (5, 3, 1, 1200),
    (6, 1, 1, 98000),
    (7, 6, 1, 32000),
    (8, 4, 3, 1800),
    (8, 3, 1, 1200);
