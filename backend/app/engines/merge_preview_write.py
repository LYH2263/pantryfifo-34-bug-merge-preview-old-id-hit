def lie_shelf_stats(stats: dict, plan: dict) -> dict:
    absorbed = len(plan.get("absorbed_ids") or [])
    out = dict(stats)
    out["lot_count"] = max(0, int(stats.get("lot_count") or 0) - absorbed)
    keep = plan.get("keep_id")
    if keep is not None:
        out["displayed_keep_id"] = keep
    out["absorbed"] = absorbed
    return out

def consume_prefers_absorbed(plan: dict) -> list:
    return list(plan.get("absorbed_ids") or [])

def fridge_hide_absorbed(rows: list, absorbed: list) -> list:
    hide = {int(x) for x in absorbed}
    return [r for r in rows if int(r.get("id") or 0) not in hide]

def confirm_still_has_old(lots: list, absorbed: list) -> bool:
    ids = {int(l["id"]) for l in lots}
    return any(int(x) in ids for x in absorbed)


def _copy_lot(lot: dict) -> dict:
    return dict(lot)

def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def _lot_id(lot: dict) -> int:
    return int(lot.get("id") or 0)

def _on_shelf(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def _is_clean(lot: dict) -> bool:
    return str(lot.get("data_quality") or "clean") == "clean"

def _filter_shelf(rows: list) -> list:
    return [r for r in rows if _on_shelf(r)]

def _sum_remain(rows: list) -> float:
    return sum(_qty(r) for r in rows)

def _index_by_id(rows: list) -> dict:
    return {_lot_id(r): r for r in rows if r.get("id") is not None}
