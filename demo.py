# -*- coding: utf-8 -*-
"""
演示入口（mini-ERP）
====================
跑法：python demo.py

会做三件事：
  1. 灌入演示数据（三级 BOM + 共用件 + 各种库存形态）
  2. 跑 MRP，输出生产建议与采购建议（含最晚开工/下单日）
  3. 演示循环 BOM 检测（故意造一个 A→B→A）
"""

import sys

import db
import mrp
import seed_data


def demo_cycle_detection():
    """演示循环 BOM 会被拦住 —— 而不是无限递归把程序跑挂"""
    print()
    print("=" * 78)
    print("循环 BOM 检测（故意造一个 P100 → PCB01 → P100）")
    print("=" * 78)

    conn = db.connect(":memory:")          # 用内存库，不污染真实数据
    db.reset(conn)
    db.add_item(conn, "P100", "控制器", "台", "finished", 3, 0)
    db.add_item(conn, "PCB01", "控制板", "块", "semi", 2, 0)
    db.add_bom(conn, "P100", "PCB01", 2)
    db.add_bom(conn, "PCB01", "P100", 1)   # ← 数据录错时会这样
    db.add_sales_order(conn, "P100", 10, "2026-11-10")
    conn.commit()

    try:
        mrp.run_mrp(conn, verbose=False)
        print("  ❌ 没有检测到循环（不应该发生）")
    except ValueError as exc:
        print(f"  ✅ 已拦截：{exc}")
    finally:
        conn.close()


def main():
    print("=" * 78)
    print("mini-ERP · MRP 运算演示")
    print("=" * 78)

    conn = db.connect()
    seed_data.seed(conn)
    print()

    mrp.run_mrp(conn, verbose=True)

    demo_cycle_detection()

    conn.close()
    print()
    print("=" * 78)
    print("说明：")
    print("  · 需齐日 = 该物料必须在哪天之前到位")
    print("  · 最晚开工/下单 = 需齐日 − 提前期（这就是 MRP 的『时间倒排』）")
    print("  · 共用件（PCB01 / SCREW）的需求已合并，来自两张订单")
    print("=" * 78)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
