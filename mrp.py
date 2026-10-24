# -*- coding: utf-8 -*-
"""
MRP 核心（Material Requirements Planning）
==========================================
跑法：由 demo.py 调用

★★ 核心观念：MRP 不是"算总需求"，是"按时间倒排"
------------------------------------------------------------
只算总量的是库存计算。真正的 MRP 要回答：

    成品 11-10 交货 → 装配周期 3 天 → 半成品必须 11-07 到
    → 半成品的原材料采购 7 天 → 原材料必须在 11-01 前下单
                                              ↑ 这叫"倒排"

所以每个需求都带两个属性：**数量** + **需求日期**。缺了日期就退化成库存计算。

★★ 三个必须处理的坑
------------------------------------------------------------
1. **共用件合并**：一颗螺丝用在三个产品上，三张订单的需求要合并成一条，
   否则会重复采购。合并时日期取**最早**的那个（最紧迫）。
2. **循环 BOM**：A 用 B、B 又用 A（数据录错时很常见），不检测会无限递归。
3. **净需求** = 毛需求 + 安全库存 − 现有 − 在途 − 未到货采购单。
   漏掉任何一项都会多买或漏买。

★★ 一个简化（诚实说明）
------------------------------------------------------------
真实 MRP 会按时间段分桶（第 1 周缺多少、第 2 周缺多少）。
本实现按**物料**归并，同一物料只保留最紧迫的需求日。
对"看完整体缺口"已经够用；要做精细排产需要改成按周分桶。
"""

import datetime

# ------------------------------------------------------------
# 读取基础数据
# ------------------------------------------------------------
def load_bom(conn):
    """返回 {父件 id: [(子件 id, 单位用量), ...]}"""
    rows = conn.execute("SELECT parent_id, child_id, qty_per FROM bom").fetchall()
    bom = {}
    for r in rows:
        bom.setdefault(r["parent_id"], []).append((r["child_id"], r["qty_per"]))
    return bom


def load_inventory(conn):
    """返回 {物料 id: {'on_hand': x, 'in_transit': y}}"""
    rows = conn.execute("SELECT * FROM inventory").fetchall()
    return {r["item_id"]: {"on_hand": r["qty_on_hand"],
                           "in_transit": r["qty_in_transit"]} for r in rows}


def load_open_purchase(conn):
    """未到货采购单 → {物料 id: 数量}"""
    rows = conn.execute(
        "SELECT item_id, SUM(qty) AS qty FROM purchase_order "
        "WHERE status = 'open' GROUP BY item_id"
    ).fetchall()
    return {r["item_id"]: r["qty"] for r in rows}


def load_open_sales(conn):
    rows = conn.execute(
        "SELECT * FROM sales_order WHERE status = 'open' ORDER BY due_date"
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------
# 核心 1：BOM 递归展开
# ------------------------------------------------------------
def explode(demands, bom, items, path=None):
    """把「成品需求」递归展开成「所有物料的需求」

    参数
      demands : {物料 id: {'qty': 数量, 'need_date': 日期}}
      bom     : {父件 id: [(子件 id, 用量), ...]}
      items   : {物料 id: 物料字典}
      path    : 当前递归路径（用于检测循环 BOM）

    返回
      {物料 id: {'qty': 毛需求, 'need_date': 需求日, 'from': 来源说明}}

    为什么用"先算需求日期、再减提前期"：这是 MRP 的时间倒排 ——
    子件必须在**父件开工之前**到齐。
    """
    path = path or []
    result = {}

    for item_id, demand in demands.items():
        _collect(result, item_id, demand["qty"], demand["need_date"],
                 note=f"直接需求")

        children = bom.get(item_id)
        if not children:
            continue                     # 叶子节点（外购件），展开结束

        if item_id in path:
            raise ValueError(
                f"检测到循环 BOM：{' → '.join(items[i]['code'] for i in path + [item_id])}"
            )

        parent = items[item_id]
        # 子件的需求日 = 父件需求日 − 父件的装配周期
        child_need = demand["need_date"] - datetime.timedelta(
            days=int(parent.get("lead_time") or 0))

        child_demands = {}
        for child_id, qty_per in children:
            _merge_demand(child_demands, child_id,
                          demand["qty"] * qty_per, child_need)

        sub = explode(child_demands, bom, items, path + [item_id])
        for sub_id, sub_d in sub.items():
            _collect(result, sub_id, sub_d["qty"], sub_d["need_date"],
                     note=f"来自 {parent['code']}")
    return result


def _merge_demand(target, item_id, qty, need_date):
    """共用件合并：数量相加，日期取最早（最紧迫）"""
    cur = target.get(item_id)
    if cur is None:
        target[item_id] = {"qty": qty, "need_date": need_date}
    else:
        cur["qty"] += qty
        cur["need_date"] = min(cur["need_date"], need_date)


def _collect(target, item_id, qty, need_date, note=""):
    _merge_demand(target, item_id, qty, need_date)


# ------------------------------------------------------------
# 核心 2：净需求 + 倒排下单/开工日
# ------------------------------------------------------------
def net_requirements(conn, gross, items, bom):
    """把毛需求换算成净需求，并倒排日期

    返回 (production_plan, purchase_plan)

    production_plan：有 BOM 的物料（成品/半成品）→ 需生产多少、最晚开工日
    purchase_plan  ：没有 BOM 的物料（外购件）→ 需采购多少、最晚下单日
    """
    inventory = load_inventory(conn)
    open_po = load_open_purchase(conn)

    production_plan = []
    purchase_plan = []

    for item_id, demand in gross.items():
        item = items[item_id]
        inv = inventory.get(item_id, {"on_hand": 0, "in_transit": 0})
        supplied = (inv["on_hand"] + inv["in_transit"] + open_po.get(item_id, 0))
        net = demand["qty"] + (item.get("safety_stock") or 0) - supplied

        if net <= 0.0001:
            continue                     # 库存够，不需要动作

        lead = int(item.get("lead_time") or 0)
        action_date = demand["need_date"] - datetime.timedelta(days=lead)

        record = {
            "code": item["code"],
            "name": item["name"],
            "unit": item["unit"],
            "item_type": item["item_type"],
            "gross": round(demand["qty"], 2),
            "on_hand": round(inv["on_hand"], 2),
            "in_transit": round(inv["in_transit"], 2),
            "open_po": round(open_po.get(item_id, 0), 2),
            "safety_stock": item.get("safety_stock") or 0,
            "net": round(net, 2),
            "need_date": demand["need_date"],
            "lead_time": lead,
            "action_date": action_date,
        }
        (production_plan if bom.get(item_id) else purchase_plan).append(record)

    production_plan.sort(key=lambda r: r["action_date"])
    purchase_plan.sort(key=lambda r: r["action_date"])
    return production_plan, purchase_plan


# ------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------
def run_mrp(conn, verbose=True):
    """跑一遍 MRP，返回 {'gross': ..., 'production': [...], 'purchase': [...]}"""
    items = load_items_safe(conn)
    bom = load_bom(conn)

    # 1. 起点：未完成销售订单
    orders = load_open_sales(conn)
    if not orders:
        if verbose:
            print("没有未完成的销售订单，无需运算。")
        return {"gross": {}, "production": [], "purchase": []}

    root = {}
    for o in orders:
        _merge_demand(root, o["item_id"], o["qty"],
                      datetime.date.fromisoformat(o["due_date"]))

    # 2. 递归展开
    gross = explode(root, bom, items)

    # 3. 净需求 + 倒排
    production, purchase = net_requirements(conn, gross, items, bom)

    if verbose:
        _print_report(orders, items, bom, gross, production, purchase)

    return {"gross": gross, "production": production, "purchase": purchase,
            "orders": orders, "items": items}


def load_items_safe(conn):
    rows = conn.execute("SELECT * FROM item").fetchall()
    return {r["id"]: dict(r) for r in rows}


# ------------------------------------------------------------
# 打印
# ------------------------------------------------------------
def _print_report(orders, items, bom, gross, production, purchase):
    print("=" * 78)
    print("未完成销售订单")
    print("=" * 78)
    for o in orders:
        it = items[o["item_id"]]
        print(f"  {o['due_date']}  交付 {it['code']} {it['name']} × {o['qty']:g} {it['unit']}")

    print()
    print("=" * 78)
    print("BOM 展开（毛需求）")
    print("=" * 78)
    for o in orders:
        _print_tree(o["item_id"], o["qty"], gross, items, bom, indent=2, prefix="")

    print()
    print("=" * 78)
    print("生产建议（有 BOM 的物料）")
    print("=" * 78)
    if production:
        print(f"  {'物料':<14}{'毛需求':>9}{'库存':>8}{'净需求':>9}{'需齐日':>12}{'提前期':>7}{'最晚开工':>12}")
        for r in production:
            print(f"  {r['code'] + ' ' + r['name']:<14}{r['gross']:>9.0f}{r['on_hand']:>8.0f}"
                  f"{r['net']:>9.0f}{str(r['need_date']):>12}{r['lead_time']:>6}天"
                  f"{str(r['action_date']):>12}")
    else:
        print("  无（库存或采购已覆盖）")

    print()
    print("=" * 78)
    print("采购建议（外购件）")
    print("=" * 78)
    if purchase:
        print(f"  {'物料':<14}{'毛需求':>9}{'库存':>8}{'在途':>7}{'在途单':>7}{'安全':>6}"
              f"{'净需求':>9}{'需齐日':>12}{'提前期':>7}{'最晚下单':>12}")
        for r in purchase:
            print(f"  {r['code'] + ' ' + r['name']:<14}{r['gross']:>9.0f}{r['on_hand']:>8.0f}"
                  f"{r['in_transit']:>7.0f}{r['open_po']:>7.0f}{r['safety_stock']:>6.0f}"
                  f"{r['net']:>9.0f}{str(r['need_date']):>12}{r['lead_time']:>6}天"
                  f"{str(r['action_date']):>12}")
    else:
        print("  无（库存已覆盖）")


def _print_tree(item_id, branch_qty, gross, items, bom, indent, prefix):
    """把 BOM 展开画成树

    ★ 显示的是**本分支用量**，不是合并后的总量
    ------------------------------------------------------------
    踩过的坑：一开始直接打印 gross[item_id]（合并后的总需求），结果
    P100 和 P200 两个子树都显示"PCB01 毛需求 250" —— 看起来像每个产品
    都要 250 块板，完全误导。
    正确做法：这一支用了多少就打多少；合并后的总量单独标注出来。
    """
    it = items[item_id]
    merged = gross.get(item_id)
    merged_qty = merged["qty"] if merged else 0

    note = ""
    if abs(merged_qty - branch_qty) > 1e-6:
        note = f"    ← 各订单合并后共 {merged_qty:g}"
    if merged is None:
        note += "    【库存已满足】"

    print(f"{' ' * indent}{prefix}{it['code']} {it['name']}  "
          f"× {branch_qty:g} {it['unit']}{note}")

    children = bom.get(item_id, [])
    for idx, (child_id, qty_per) in enumerate(children):
        last = idx == len(children) - 1
        _print_tree(child_id, branch_qty * qty_per, gross, items, bom,
                    indent + 4, "└─ " if last else "├─ ")
