# -*- coding: utf-8 -*-
"""
数据库层（mini-ERP）
====================
职责：建表、连接、基础读写。

设计原则：**6 张表，能不加就不加。**
ERP 真正的复杂度不在表的数量，而在"单据之间的约束"（库存不能凭空多、
需求要能追溯来源）。所以表少但字段和约束要考虑清楚。

表之间的关系：

    sales_order ──需求──┐
                        ├──> MRP 展开 ──> purchase_order（采购建议）
    bom ──结构──────────┘
    inventory ──供给────────┘
    inventory_txn ──审计（每一次库存变动都要留痕）
"""

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ERP_DB", os.path.join(BASE_DIR, "erp.db"))

SCHEMA = """
-- 物料主数据：成品 / 半成品 / 原材料，统一放一张表
CREATE TABLE IF NOT EXISTS item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT UNIQUE NOT NULL,     -- 物料编码，业务唯一键
    name          TEXT NOT NULL,
    unit          TEXT,                     -- 单位：台 / 件 / kg
    item_type     TEXT NOT NULL,            -- finished / semi / raw
    lead_time     INTEGER DEFAULT 0,        -- 提前期（天）：采购周期或装配周期
    safety_stock  INTEGER DEFAULT 0         -- 安全库存
);

-- 物料清单（BOM）：一个父件由多少个某子件组成
CREATE TABLE IF NOT EXISTS bom (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL,
    child_id  INTEGER NOT NULL,
    qty_per   REAL NOT NULL,                -- 单位用量（可能是小数，如 0.5 kg）
    UNIQUE (parent_id, child_id),           -- 同一个父件下，同一子件只出现一次
    FOREIGN KEY (parent_id) REFERENCES item(id),
    FOREIGN KEY (child_id)  REFERENCES item(id)
);

-- 库存：现有量与在途量分开记（在途=已下单未到货）
CREATE TABLE IF NOT EXISTS inventory (
    item_id        INTEGER PRIMARY KEY,
    qty_on_hand    REAL DEFAULT 0,          -- 现有库存
    qty_in_transit REAL DEFAULT 0,          -- 在途数量
    FOREIGN KEY (item_id) REFERENCES item(id)
);

-- 销售订单：需求来源
CREATE TABLE IF NOT EXISTS sales_order (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   INTEGER NOT NULL,
    qty       REAL NOT NULL,
    due_date  TEXT NOT NULL,                -- 交货日期 YYYY-MM-DD
    status    TEXT DEFAULT 'open',          -- open / closed
    FOREIGN KEY (item_id) REFERENCES item(id)
);

-- 采购单：已经开出去的，未到货的部分要参与抵扣
CREATE TABLE IF NOT EXISTS purchase_order (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL,
    qty           REAL NOT NULL,
    order_date    TEXT,                     -- 下单日
    expected_date TEXT,                     -- 预计到货日
    status        TEXT DEFAULT 'open',      -- open / received
    FOREIGN KEY (item_id) REFERENCES item(id)
);

-- 库存流水：每一次变动都留痕（ERP 里叫"单据流"，是审计的根）
CREATE TABLE IF NOT EXISTS inventory_txn (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL,
    qty_delta  REAL NOT NULL,               -- 正数入、负数出
    reason     TEXT,                        -- purchase_in / issue / adjust
    ref_type   TEXT,                        -- 关联单据类型
    ref_id     INTEGER,                     -- 关联单据 id
    created_at TEXT,
    FOREIGN KEY (item_id) REFERENCES item(id)
);
"""


def connect(db_path=None):
    """打开连接并确保表存在"""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def reset(conn):
    """清空所有业务数据（保留表结构），方便反复跑演示"""
    for table in ("inventory_txn", "purchase_order", "sales_order",
                  "inventory", "bom", "item"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                 "('item','bom','sales_order','purchase_order','inventory_txn')")
    conn.commit()


def add_item(conn, code, name, unit, item_type, lead_time=0, safety_stock=0):
    cur = conn.execute(
        "INSERT INTO item (code, name, unit, item_type, lead_time, safety_stock) "
        "VALUES (?,?,?,?,?,?)",
        (code, name, unit, item_type, lead_time, safety_stock),
    )
    return cur.lastrowid


def add_bom(conn, parent_code, child_code, qty_per):
    pid = get_item_id(conn, parent_code)
    cid = get_item_id(conn, child_code)
    conn.execute(
        "INSERT INTO bom (parent_id, child_id, qty_per) VALUES (?,?,?)",
        (pid, cid, qty_per),
    )


def add_sales_order(conn, item_code, qty, due_date):
    iid = get_item_id(conn, item_code)
    conn.execute(
        "INSERT INTO sales_order (item_id, qty, due_date, status) VALUES (?,?,?,'open')",
        (iid, qty, due_date),
    )


def add_purchase_order(conn, item_code, qty, order_date, expected_date, status="open"):
    iid = get_item_id(conn, item_code)
    conn.execute(
        "INSERT INTO purchase_order (item_id, qty, order_date, expected_date, status) "
        "VALUES (?,?,?,?,?)",
        (iid, qty, order_date, expected_date, status),
    )


def get_item_id(conn, code):
    row = conn.execute("SELECT id FROM item WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise KeyError(f"物料不存在：{code}")
    return row["id"]


def load_items(conn):
    """返回 {id: 物料字典}"""
    rows = conn.execute("SELECT * FROM item").fetchall()
    return {r["id"]: dict(r) for r in rows}

def stock_in(conn, item_code, qty, reason="purchase_in"):
    """入库：记一条流水，同时把余额加上去"""
    iid = get_item_id(conn, item_code)

    # 1) 如果还没有这个物料的余额行，先建一行（余额 0）
    row = conn.execute("SELECT item_id FROM inventory WHERE item_id = ?", (iid,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO inventory (item_id, qty_on_hand, qty_in_transit) VALUES (?, 0, 0)",
            (iid,),
        )

    # 2) 记一条流水
    conn.execute(
        "INSERT INTO inventory_txn (item_id, qty_delta, reason, ref_type, ref_id, created_at) "
        "VALUES (?, ?, ?, NULL, NULL, datetime('now','localtime'))",
        (iid, qty, reason),
    )

    # 3) 更新余额
    conn.execute(
        "UPDATE inventory SET qty_on_hand = qty_on_hand + ? WHERE item_id = ?",
        (qty, iid),
    )
    conn.commit()

def stock_out(conn, item_code, qty, reason="issue"):
    """出库：记一条负流水，同时把余额减掉；库存不足要报错"""
    iid = get_item_id(conn, item_code)

    row = conn.execute(
        "SELECT qty_on_hand FROM inventory WHERE item_id = ?", (iid,)
    ).fetchone()
    on_hand = row["qty_on_hand"] if row else 0

    # ← 这段我写好了，你读一下它干什么
    if on_hand < qty:
        raise ValueError(f"库存不足：{item_code} 现有 {on_hand}，需要出库 {qty}")

    conn.execute(
        "INSERT INTO inventory_txn (item_id, qty_delta, reason, ref_type, ref_id, created_at) "
        "VALUES (?, ?, ?, NULL, NULL, datetime('now','localtime'))",
        (iid, -qty, reason),
    )

    conn.execute(
        "UPDATE inventory SET qty_on_hand = qty_on_hand - ? WHERE item_id = ?",
        (qty, iid),
    )
    conn.commit()

def stock_adjust(conn, item_code, qty_delta, reason="adjust"):
    """库存调整：正数加、负数减；库存不足要报错

    与 stock_in / stock_out 的区别：那两个是"有明确业务含义的变动"
    （采购入库、生产领用），这里是"盘点差异"，所以用差额表达。
    """
    iid = get_item_id(conn, item_code)

    # 1) 确保余额行存在
    #    INSERT OR IGNORE = "有就跳过，没有才插"（主键冲突时什么都不做）
    #    ★ 这一步必须在 UPDATE 之前：先有行，UPDATE 才能匹配到
    conn.execute(
        "INSERT OR IGNORE INTO inventory (item_id, qty_on_hand, qty_in_transit) "
        "VALUES (?, 0, 0)",
        (iid,),
    )

    # 2) 此时一定有一行，读出现有余额
    on_hand = conn.execute(
        "SELECT qty_on_hand FROM inventory WHERE item_id = ?", (iid,)
    ).fetchone()[0]

    # 3) 调整后不能是负数
    if on_hand + qty_delta < 0:
        raise ValueError(f"库存不足：{item_code} 现有 {on_hand}，需要调整 {qty_delta}")

    conn.execute(
        "INSERT INTO inventory_txn (item_id, qty_delta, reason, ref_type, ref_id, created_at) "
        "VALUES (?, ?, ?, NULL, NULL, datetime('now','localtime'))",
        (iid, qty_delta, reason),
    )

    conn.execute(
        "UPDATE inventory SET qty_on_hand = qty_on_hand + ? WHERE item_id = ?",
        (qty_delta, iid),
    )
    conn.commit()