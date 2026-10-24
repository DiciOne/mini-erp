# 核心代码导读

> 这份文档只讲 **3 个函数、约 90 行**。其余 200 行是读数据、打印表格、排序 —— **不用看**。
>
> 读完之后你应该能回答文档最后的 3 道自测题。

---

## 读之前：MRP 就三件事

```
1. 展开   —— 成品要 100 台，那它下面的零件各要多少？（BOM 递归）
2. 合并   —— 同一个零件被两个产品用到，需求加在一起（共用件）
3. 倒排   —— 什么时候必须下单？= 需求日 − 提前期
```

**只做第 1 件的叫"拆料单"，三件都做了才叫 MRP。**

---

# 一、`explode()` —— BOM 递归展开

**它在干什么**：给一个"成品需求"，算出"所有物料各需要多少、什么时候需要"。

```python
def explode(demands, bom, items, path=None):
    path = path or []          # ① 递归路径，用来检测循环 BOM
    result = {}                # ② 最终结果：{物料id: {数量, 需求日}}

    for item_id, demand in demands.items():
        # ③ 先把这个物料自己的需求记进结果
        _collect(result, item_id, demand["qty"], demand["need_date"])

        children = bom.get(item_id)
        if not children:
            continue           # ④ 没有 BOM = 外购件（叶子），展开到此结束

        # ⑤ 循环检测：如果"我"已经在当前路径里，说明 A→B→A 成环了
        if item_id in path:
            raise ValueError("检测到循环 BOM：...")

        parent = items[item_id]

        # ⑥ ★ 时间倒排：子件必须在父件"开工之前"到齐
        #    所以子件需求日 = 父件需求日 − 父件的装配周期
        child_need = demand["need_date"] - datetime.timedelta(
            days=int(parent.get("lead_time") or 0))

        # ⑦ 把父件需求按单位用量拆给每个子件
        #    父件要 100 台，控制板用量 2 → 子件需求 200 块
        child_demands = {}
        for child_id, qty_per in children:
            _merge_demand(child_demands, child_id,
                          demand["qty"] * qty_per, child_need)

        # ⑧ 递归展开子件，把子件算出来的需求合并回总结果
        sub = explode(child_demands, bom, items, path + [item_id])
        for sub_id, sub_d in sub.items():
            _collect(result, sub_id, sub_d["qty"], sub_d["need_date"])

    return result
```

## 三个关键设计（面试就问这三个）

| # | 设计 | 为什么必须这样 |
|---|---|---|
| ⑥ | 子件需求日 = 父件需求日 − 父件提前期 | **这就是 MRP 和"拆料单"的区别。** 不减提前期，就只能算出总量，算不出"什么时候要" |
| ⑤ | 递归前检查 `item_id in path` | BOM 数据录错成 A→B→A 时，没有这个检查会**无限递归把程序跑挂** |
| ④ | 没有子件就 `continue` | 叶子节点是外购件，展开到它就该停 —— 它没有"下一层" |

---

# 二、`_merge_demand()` —— 共用件合并（只有 8 行）

**它在干什么**：同一个零件被多个父件用到时，把需求加在一起。

```python
def _merge_demand(target, item_id, qty, need_date):
    cur = target.get(item_id)

    if cur is None:
        # 第一次出现：直接记录
        target[item_id] = {"qty": qty, "need_date": need_date}
    else:
        # 已经出现过：数量相加
        cur["qty"] += qty
        # ★ 日期取最早（最紧迫）的那一个
        cur["need_date"] = min(cur["need_date"], need_date)
```

## 为什么日期要取 `min`

假设控制板 PCB01 同时用在两个产品上：

| 来源 | 数量 | 需求日 |
|---|---|---|
| P100 控制器（11-10 交货，装配 3 天） | 200 | **11-07** |
| P200 驱动器（11-20 交货，装配 2 天） | 50 | 11-18 |

**合并后：250 块，需求日必须取 11-07。**

> 如果取 11-18（最晚），就会按"11-18 到货"去下单 —— **为 P100 准备的料会晚到 11 天，那张订单必然延期。**

**这就是"最小日期"背后的业务含义：料要满足最紧迫的那张单。**

---

# 三、`net_requirements()` —— 净需求 + 时间倒排

**它在干什么**：把"毛需求"（该用多少）换算成"净需求"（还差多少买多少），并算出最晚下单日。

```python
def net_requirements(conn, gross, items, bom):
    inventory = load_inventory(conn)        # 现有库存 + 在途
    open_po = load_open_purchase(conn)      # 未到货的采购单

    production_plan = []   # 有 BOM 的物料 → 生产建议
    purchase_plan = []     # 没 BOM 的物料 → 采购建议

    for item_id, demand in gross.items():
        item = items[item_id]

        # ★ 供给 = 现有 + 在途 + 已下单未到货
        inv = inventory.get(item_id, {"on_hand": 0, "in_transit": 0})
        supplied = inv["on_hand"] + inv["in_transit"] + open_po.get(item_id, 0)

        # ★ 净需求 = 毛需求 + 安全库存 − 供给
        net = demand["qty"] + (item.get("safety_stock") or 0) - supplied

        if net <= 0.0001:
            continue        # 库存够，不需要动作（用 0.0001 是为了躲开浮点误差）

        # ★ 时间倒排：最晚动作日 = 需求日 − 提前期
        lead = int(item.get("lead_time") or 0)
        action_date = demand["need_date"] - datetime.timedelta(days=lead)

        record = { ... }    # 把明细存成一条记录（方便打印/核对）

        # ★ 分桶：有 BOM 的要"生产"，没 BOM 的要"采购"
        (production_plan if bom.get(item_id) else purchase_plan).append(record)

    production_plan.sort(key=lambda r: r["action_date"])   # 按紧急度排序
    purchase_plan.sort(key=lambda r: r["action_date"])
    return production_plan, purchase_plan
```

## 那个公式，少一项就会买错

```
净需求 = 毛需求 + 安全库存 − 现有库存 − 在途 − 未到货采购单
                    ↑           ↑         ↑          ↑
                 保底量      仓库有     路上有    已下单没到
```

| 漏掉哪一项 | 后果 |
|---|---|
| 漏掉**在途** | 重复采购（钱压两遍） |
| 漏掉**未到货采购单** | 重复采购（同一个东西下两次单） |
| 漏掉**安全库存** | 用光库存就停产 |

## 用 `0.0001` 而不是 `0` 的原因

`qty` 字段是 `REAL`（浮点数）。`100.0 - 100.0` 在某些情况下会得到 `1e-14` 而不是精确的 `0`。

**如果用 `if net <= 0`，这种"数学上等于 0 但浮点上不精确"的情况会被误判成"还缺料"。** 所以留一个极小的容差。

---

# 读完自测（3 道题）

<details>
<summary>1. 为什么子件的需求日要"减去父件的提前期"，而不是"加上"？</summary>

因为时间是从交货日**往前**倒推的。父件必须先到，子件才能装配 ——
所以子件到货时间一定**早于**父件开工时间。减，方向才是往前。
</details>

<details>
<summary>2. 共用件合并时，日期为什么取 min 而不是 max？</summary>

取 max（最晚）会导致为最紧迫订单准备的料晚到，那张单必然延期。
取 min（最早）才能保证所有用到它的订单都不缺料。
</details>

<details>
<summary>3. 净需求要不要减掉"未到货的采购单"？为什么？</summary>

要。未到货采购单意味着"这部分料已经在路上了"，如果不减，
系统会认为还缺这么多，导致**重复下单**。
</details>

---

# 一句话总结

> **MRP = 递归展开（算出每个物料要多少）+ 共用件合并（同料累加、日期取最早）+ 净需求五项抵扣 + 按提前期倒排（算出最晚什么时候动手）。**

这四件事，就是 `explode()` + `_merge_demand()` + `net_requirements()` 在做的事。
