# -*- coding: utf-8 -*-
"""
MRP 核心回归测试
================
跑法：pytest test_mrp.py -v

全部用内存库，不碰 erp.db，秒级跑完。

测的是四个最容易错的地方：
  1. 共用件合并（同一物料出现在两个产品里，需求要相加、日期取最早）
  2. 净需求公式（毛需求 + 安全库存 − 现有 − 在途 − 未到货采购单）
  3. 时间倒排（需齐日 − 提前期 = 最晚下单/开工日）
  4. 循环 BOM 必须被拦住，而不是无限递归
"""

import datetime

import pytest

import db
import mrp
import seed_data


@pytest.fixture
def conn():
    """每个用例一个干净的内存库"""
    c = db.connect(":memory:")
    seed_data.seed(c)
    yield c
    c.close()


def by_code(plan):
    return {r["code"]: r for r in plan}


def test_shared_component_merged(conn):
    """共用件：PCB01 同时用于 P100(×2) 和 P200(×1)，需求必须合并

    P100 100 台 × 2 = 200，P200 50 台 × 1 = 50，合计 250
    """
    result = mrp.run_mrp(conn, verbose=False)
    gross = result["gross"]
    items = result["items"]
    pcb_id = db.get_item_id(conn, "PCB01")
    assert gross[pcb_id]["qty"] == 250

    # 螺丝同理：100×8 + 50×4 = 1000
    screw_id = db.get_item_id(conn, "SCREW")
    assert gross[screw_id]["qty"] == 1000


def test_merged_need_date_takes_earliest(conn):
    """合并需求时日期取最早（最紧迫）

    PCB01 来自 P100 是 11-07（11-10 − 3天），来自 P200 是 11-18（11-20 − 2天）
    → 合并后必须取 11-07
    """
    result = mrp.run_mrp(conn, verbose=False)
    pcb_id = db.get_item_id(conn, "PCB01")
    assert result["gross"][pcb_id]["need_date"] == datetime.date(2026, 11, 7)


def test_net_requirement_formula(conn):
    """净需求 = 毛需求 + 安全库存 − 现有 − 在途 − 未到货采购单

    CHIP01：毛需求 750，安全库存 50，现有 300 → 净需求 500
    MOTOR01：毛需求 50，现有 0，未到货采购单 20 → 净需求 30
    """
    result = mrp.run_mrp(conn, verbose=False)
    purchase = by_code(result["purchase"])

    assert purchase["CHIP01"]["net"] == 500
    assert purchase["CHIP01"]["safety_stock"] == 50
    assert purchase["MOTOR01"]["net"] == 30
    assert purchase["MOTOR01"]["open_po"] == 20


def test_time_phasing(conn):
    """时间倒排：最晚下单日 = 需齐日 − 提前期

    PCB01 需齐 11-07，装配 2 天 → 最晚开工 11-05
    CHIP01 需齐 11-05（PCB01 开工前），采购 14 天 → 最晚下单 10-22
    """
    result = mrp.run_mrp(conn, verbose=False)
    prod = by_code(result["production"])
    purchase = by_code(result["purchase"])

    assert prod["PCB01"]["action_date"] == datetime.date(2026, 11, 5)
    assert purchase["CHIP01"]["action_date"] == datetime.date(2026, 10, 22)
    assert purchase["CHIP01"]["need_date"] == datetime.date(2026, 11, 5)


def test_sufficient_stock_produces_no_suggestion(conn):
    """库存够的物料不该出现在建议里"""
    conn.execute("UPDATE inventory SET qty_on_hand = 5000 WHERE item_id = "
                 "(SELECT id FROM item WHERE code = 'SCREW')")
    conn.commit()
    result = mrp.run_mrp(conn, verbose=False)
    purchase = by_code(result["purchase"])
    assert "SCREW" not in purchase


def test_cycle_bom_raises():
    """循环 BOM 必须报错，不能无限递归"""
    c = db.connect(":memory:")
    db.reset(c)
    db.add_item(c, "A", "甲", "个", "finished", 1, 0)
    db.add_item(c, "B", "乙", "个", "semi", 1, 0)
    db.add_bom(c, "A", "B", 1)
    db.add_bom(c, "B", "A", 1)          # ← 环
    # 库存不写也行 —— 没有余额行时 MRP 按 0 处理
    db.add_sales_order(c, "A", 10, "2026-11-10")
    c.commit()

    with pytest.raises(ValueError, match="循环 BOM"):
        mrp.run_mrp(c, verbose=False)
    c.close()


def test_no_open_orders_returns_empty(conn):
    """没有未完成订单时不该报错，返回空计划"""
    conn.execute("UPDATE sales_order SET status = 'closed'")
    conn.commit()
    result = mrp.run_mrp(conn, verbose=False)
    assert result["production"] == []
    assert result["purchase"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
