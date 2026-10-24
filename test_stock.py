# -*- coding: utf-8 -*-
"""
库存流水测试
============
跑法：pytest test_stock.py -v

锁住的核心不变量：

    inventory.qty_on_hand  ==  SUM(inventory_txn.qty_delta)    （每个物料）

为什么这个等式这么重要：MRP 的缺料判断、采购建议全部建立在库存余额上。
等式一旦不成立，所有建议都是假的。而它被破坏的方式往往很隐蔽 ——
只要留一个"直接 UPDATE 余额"的后门，绕过流水改一次数就崩了。
"""

import pytest

import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.add_item(c, "A", "测试物料", "个", "raw", 5, 0)
    c.commit()
    yield c
    c.close()


def balance(conn, code):
    iid = db.get_item_id(conn, code)
    row = conn.execute(
        "SELECT qty_on_hand FROM inventory WHERE item_id = ?", (iid,)
    ).fetchone()
    return row[0] if row else 0.0


def txn_sum(conn, code):
    iid = db.get_item_id(conn, code)
    return conn.execute(
        "SELECT COALESCE(SUM(qty_delta), 0) FROM inventory_txn WHERE item_id = ?",
        (iid,),
    ).fetchone()[0]


def test_stock_in_records_txn_and_balance(conn):
    """入库：流水 + 余额都要动，且两者一致"""
    db.stock_in(conn, "A", 100, reason="purchase_in")
    assert balance(conn, "A") == 100
    assert txn_sum(conn, "A") == 100

    # 再入一次必须是累加，不是覆盖
    db.stock_in(conn, "A", 50, reason="purchase_in")
    assert balance(conn, "A") == 150


def test_stock_out_records_negative_txn(conn):
    """出库：流水要记负数，余额要减"""
    db.stock_in(conn, "A", 100, reason="opening")
    db.stock_out(conn, "A", 30, reason="issue")
    assert balance(conn, "A") == 70
    assert txn_sum(conn, "A") == 70


def test_stock_out_rejects_insufficient(conn):
    """库存不足必须报错，不能把余额做成负数"""
    db.stock_in(conn, "A", 10, reason="opening")
    with pytest.raises(ValueError, match="库存不足"):
        db.stock_out(conn, "A", 999)
    # 报错之后余额不能被改动
    assert balance(conn, "A") == 10
    assert txn_sum(conn, "A") == 10


def test_invariant_holds_for_all_items():
    """整个演示数据集：每个物料的余额都必须等于流水汇总

    这条测试是"后门探测仪" —— 只要有人绕过流水直接改 inventory，
    它立刻会红。
    """
    import seed_data

    conn = db.connect(":memory:")
    seed_data.seed(conn)

    bad = []
    for r in conn.execute("SELECT code, id FROM item"):
        row = conn.execute(
            "SELECT qty_on_hand FROM inventory WHERE item_id = ?", (r["id"],)
        ).fetchone()
        bal = row[0] if row else 0.0
        tx = conn.execute(
            "SELECT COALESCE(SUM(qty_delta), 0) FROM inventory_txn WHERE item_id = ?",
            (r["id"],),
        ).fetchone()[0]
        if abs(bal - tx) > 1e-6:
            bad.append((r["code"], bal, tx))

    conn.close()
    assert not bad, f"以下物料的余额与流水不一致（存在绕过流水的写入）：{bad}"


def test_opening_stock_is_a_transaction():
    """期初库存也必须是一条流水，不能凭空出现在余额表里"""
    import seed_data

    conn = db.connect(":memory:")
    seed_data.seed(conn)

    rows = conn.execute(
        "SELECT reason, COUNT(*) AS n FROM inventory_txn GROUP BY reason"
    ).fetchall()
    reasons = {r["reason"]: r["n"] for r in rows}
    conn.close()

    assert reasons.get("opening", 0) == 4, f"期初流水应有 4 条，实际 {reasons}"


def test_adjust_on_item_without_balance_row():
    """物料还没有余额行时，stock_adjust 也必须同时改流水和余额

    ★ 这是一个真实 bug 的回归用例（2026-10-24 发现）：
      stock_adjust 直接 UPDATE inventory，但新物料在 inventory 表里
      还没有行 → UPDATE 匹配 0 行 → 流水记了、余额没动 → 不变量被破坏。
      stock_in 里处理了这个边界，stock_adjust 漏了。
    """
    conn = db.connect(":memory:")
    db.add_item(conn, "A", "料", "个", "raw", 5, 0)
    conn.commit()          # 故意不建 inventory 行

    db.stock_adjust(conn, "A", 100, reason="adjust")

    row = conn.execute(
        "SELECT qty_on_hand FROM inventory WHERE item_id = 1").fetchone()
    bal = row[0] if row else 0
    tx = conn.execute(
        "SELECT COALESCE(SUM(qty_delta), 0) FROM inventory_txn WHERE item_id = 1"
    ).fetchone()[0]
    conn.close()
    assert abs(bal - tx) < 1e-6, "余额和流水不一致 —— 又出现了绕过流水的写入"


def test_adjust_when_balance_row_exists():
    """余额行【已存在】时，stock_adjust 也必须能正常跑

    ★ 回归用例（2026-10-24 第二个 bug）：
      修上面那个"余额行不存在"的 bug 时，变量 row 被复用了 ——
      row 来自 "SELECT item_id"，没有 qty_on_hand 列，
      所以对已有余额行取 row["qty_on_hand"] 会抛 IndexError。

      当时 13 个测试全绿也没发现，因为没有用例覆盖
      "余额行已存在 + 调用 adjust" 这个组合。
      → 教训：修一个路径的 bug 时，必须补测"另一条路径"。
    """
    conn = db.connect(":memory:")
    db.add_item(conn, "A", "料", "个", "raw", 5, 0)
    conn.commit()
    db.stock_in(conn, "A", 100, reason="opening")      # 先把余额行建出来

    db.stock_adjust(conn, "A", 20, reason="adjust")    # ← 这里会炸

    row = conn.execute(
        "SELECT qty_on_hand FROM inventory WHERE item_id = 1").fetchone()
    tx = conn.execute(
        "SELECT COALESCE(SUM(qty_delta), 0) FROM inventory_txn WHERE item_id = 1"
    ).fetchone()[0]
    conn.close()
    assert row[0] == 120, f"余额应为 120，实际 {row[0]}"
    assert abs(row[0] - tx) < 1e-6, "余额与流水不一致"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
