"""同品同到期日合并（merge）。

规则（与 FEFO 消费的隔离资格一致）：
- 必须同一 item_id；
- 必须同一 expiry（到期日不同整单失败，全部回到合并前）；
- data_quality 必须一致（dirty 批与 clean 批不得并进同一条）；
- 所有批次必须 on_shelf 且 qty_remain > 0；
- 合并后只留一条（survivor），余量 = 合并前之和，不新建任何行；
- survivor 取 FEFO 排序键 (expiry, id) 最小者 —— 即同到期日下 id 最小的在架批，
  这样合并确认后按临期消费只能打到留下来的那条。
"""

from __future__ import annotations

from app.engines.fefo import is_deduction_candidate, sort_lots_fefo


def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain", 0))


def plan_merge(lots: list[dict]) -> dict:
    """对候选批次做合并校验与规划，纯函数、不改入参。

    返回::

        {"ok": True, survivor_id, absorbed_ids, total_qty, item_id, expiry, data_quality}
        {"ok": False, "reason": "..."}

    0/1 条、重复 id、异品、异期、异隔离资格、非在架/非正余量均失败。
    """
    if not isinstance(lots, list) or len(lots) < 2:
        return {"ok": False, "reason": "need_two_lots"}
    ids = [l.get("id") for l in lots]
    if any(i is None for i in ids) or len(set(ids)) != len(ids):
        # 同一条被选两次 / 来源不可信 —— 不允许，避免把余量重复计入。
        return {"ok": False, "reason": "duplicate_lot"}
    item_ids = {l.get("item_id") for l in lots}
    if len(item_ids) != 1:
        return {"ok": False, "reason": "item_mismatch"}
    expiries = {l.get("expiry") for l in lots}
    if len(expiries) != 1:
        return {"ok": False, "reason": "expiry_mismatch"}
    qualities = {l.get("data_quality", "clean") for l in lots}
    if len(qualities) != 1:
        # 脏批与干净批不得并进同一条：survivor 只有一个 data_quality，
        # 混并后扣减门无法再区分来源。
        return {"ok": False, "reason": "quality_mismatch"}
    for l in lots:
        # 与扣减候选同一套门：不在架或非正余量的批既不能扣减也不能合并。
        if not is_deduction_candidate(l):
            return {"ok": False, "reason": "lot_not_mergeable"}
    total = round(sum(_qty(l) for l in lots), 6)
    if total <= 0:
        return {"ok": False, "reason": "lot_not_mergeable"}
    ordered = sort_lots_fefo(lots)  # 同 expiry 时按 id 升序
    survivor = ordered[0]
    absorbed = ordered[1:]
    return {
        "ok": True,
        "survivor_id": survivor["id"],
        "absorbed_ids": [l["id"] for l in absorbed],
        "total_qty": total,
        "item_id": lots[0]["item_id"],
        "expiry": lots[0]["expiry"],
        "data_quality": lots[0].get("data_quality", "clean"),
    }
