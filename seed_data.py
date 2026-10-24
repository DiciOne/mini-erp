# -*- coding: utf-8 -*-
"""
演示数据（mini-ERP）
====================
刻意造得能暴露三个关键场景：

1. **三级 BOM**：成品 → 半成品 → 原材料
2. **共用件**：控制板 PCB01 和螺丝 SCREW 同时用在两个产品上
   → 需求必须合并，否则会重复采购
3. **各种供给形态**：有库存的、有在途的、有未到货采购单的、什么都没有的
"""

import db

# (编码, 名称, 单位, 类型, 提前期天数, 安全库存)
ITEMS = [
    ("P100", "控制器", "台", "finished", 3, 0),
    ("P200", "驱动器", "台", "finished", 2, 0),
    ("PCB01", "控制板", "块", "semi", 2, 0),
    ("CHIP01", "主控芯片", "片", "raw", 14, 50),
    ("RES10K", "电阻10K", "个", "raw", 5, 0),
    ("SCREW", "M3 螺丝", "个", "raw", 3, 0),
    ("CASE01", "机箱", "个", "raw", 7, 0),
    ("MOTOR01", "伺服电机", "台", "raw", 10, 0),
]

# (父件, 子件, 单位用量)
BOM = [
    ("P100", "PCB01", 2),
    ("P100", "SCREW", 8),
    ("P100", "CASE01", 1),
    ("P200", "PCB01", 1),      # ← 与 P100 共用控制板
    ("P200", "SCREW", 4),      # ← 与 P100 共用螺丝
    ("P200", "MOTOR01", 1),
    ("PCB01", "CHIP01", 3),
    ("PCB01", "RES10K", 40),
]

# (编码, 现有库存, 在途)
INVENTORY = [
    ("CHIP01", 300, 0),        # 有库存但远远不够
    ("RES10K", 2000, 0),
    ("SCREW", 900, 0),         # 库存刚好差一点点
    ("CASE01", 30, 0),
    ("MOTOR01", 0, 0),
    ("PCB01", 0, 0),
    ("P100", 0, 0),
    ("P200", 0, 0),
]

# (编码, 数量, 交期)
SALES_ORDERS = [
    ("P100", 100, "2026-11-10"),
    ("P200", 50, "2026-11-20"),
]

# (编码, 数量, 下单日, 预计到货日)
PURCHASE_ORDERS = [
    ("MOTOR01", 20, "2026-10-15", "2026-11-05"),   # 在途 20 台，可抵扣需求
]


def seed(conn):
    """清空并重新灌入演示数据"""
    db.reset(conn)

    for code, name, unit, itype, lead, safety in ITEMS:
        db.add_item(conn, code, name, unit, itype, lead, safety)
    for parent, child, qty_per in BOM:
        db.add_bom(conn, parent, child, qty_per)
    # 期初库存也走流水 —— 不留"直接改余额"的后门
    for code, on_hand, _in_transit in INVENTORY:
        if on_hand:
            db.stock_in(conn, code, on_hand, reason="opening")
    for code, qty, due in SALES_ORDERS:
        db.add_sales_order(conn, code, qty, due)
    for code, qty, order_date, expected in PURCHASE_ORDERS:
        db.add_purchase_order(conn, code, qty, order_date, expected)

    conn.commit()
    print(f"✅ 演示数据已灌入：{len(ITEMS)} 个物料 / {len(BOM)} 条 BOM 关系 / "
          f"{len(SALES_ORDERS)} 张销售订单 / {len(PURCHASE_ORDERS)} 张采购单")
