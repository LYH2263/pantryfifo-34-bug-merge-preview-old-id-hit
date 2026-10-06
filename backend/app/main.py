import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.engines.merge import plan_merge

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        # 先取写锁再读：与合并确认互斥。锁内看到的 on_shelf 正余量批次
        # 一定是当前真实状态，扣减只会引用仍然存在的 lot id。
        c.execute("BEGIN IMMEDIATE")
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            c.rollback(); raise HTTPException(400, result["reason"])
        if not result["ok"]:
            c.rollback(); raise HTTPException(409, result)
        for d in result["deductions"]:
            cur = c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=? AND status='on_shelf'",
                            (d["take"], d["lot_id"]))
            if cur.rowcount != 1:
                # 批在规划后被并发合并吞掉 —— 整单失败、余量不动。
                c.rollback(); raise HTTPException(409, {"ok": False, "reason": "lot_changed"})
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem < -1e-9:
                # 并发改量导致库存不足：整单回滚回到消费前。
                c.rollback(); raise HTTPException(409, {"ok": False, "reason": "short"})
            if rem <= 1e-9:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit()
    finally:
        c.close()
    return result

class MergeIn(BaseModel):
    lot_ids: list[int]

def _merge_group_key(ids: list[int]) -> str:
    return ",".join(str(i) for i in sorted(ids))

def _shelf_stats(c) -> dict:
    r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(qty_remain),0) q FROM lots WHERE status='on_shelf'").fetchone()
    return {"lot_count": r["n"], "total_qty": round(float(r["q"]), 6)}

@app.post("/api/merge/preview")
def merge_preview(body: MergeIn):
    """只读：规划合并并回传当前全层真实条数/余量。不产生任何写入。

    预览阶段库存未动，报的就是合并前的数字；确认后条数才会减。
    回包里不携带“扣减偏好批号” —— 合并未确认前，FEFO 该打谁由真实在架批决定。
    """
    c = connect()
    try:
        lots = [dict(r) for r in c.execute(
            f"SELECT * FROM lots WHERE id IN ({','.join('?' * len(body.lot_ids))})", body.lot_ids)] \
            if body.lot_ids else []
        plan = plan_merge(lots)
        stats = _shelf_stats(c)
    finally:
        c.close()
    if not plan["ok"]:
        raise HTTPException(409, plan)
    return {"plan": plan, "shelf_before": stats, "lots": lots}

@app.post("/api/merge/confirm")
def merge_confirm(body: MergeIn):
    c = connect()
    try:
        # 写锁内重读，与消费互斥：先拿锁的一方决定最终状态，后到方按新状态重规划。
        c.execute("BEGIN IMMEDIATE")
        key = _merge_group_key(body.lot_ids)
        prior = c.execute("SELECT result_json FROM merges WHERE group_key=?", (key,)).fetchone()
        if prior:
            # 同一组批重复确认：直接回放上次结果，余量不再加一遍。
            c.rollback()
            return {**json.loads(prior["result_json"]), "idempotent": True}
        lots = [dict(r) for r in c.execute(
            f"SELECT * FROM lots WHERE id IN ({','.join('?' * len(body.lot_ids))})", body.lot_ids)] \
            if body.lot_ids else []
        plan = plan_merge(lots)
        if not plan["ok"]:
            # 异期/异品/脏净混批/批已不在架：整单失败，全部回到合并前，不产生幽灵行。
            c.rollback(); raise HTTPException(409, plan)
        total = plan["total_qty"]
        # 写前在同一把锁内测一次合并前真实数字 —— 与预览、确认页回包同一来源。
        before = _shelf_stats(c)
        # survivor 直接置为合并总量（而非自增），天然不会重复累加。
        cur = c.execute(
            "UPDATE lots SET qty_remain=? WHERE id=? AND status='on_shelf' AND qty_remain>0",
            (total, plan["survivor_id"]))
        if cur.rowcount != 1:
            c.rollback(); raise HTTPException(409, {"ok": False, "reason": "lot_changed"})
        for lid in plan["absorbed_ids"]:
            cur = c.execute(
                "UPDATE lots SET status='merged', qty_remain=0 WHERE id=? AND status='on_shelf'",
                (lid,))
            if cur.rowcount != 1:
                c.rollback(); raise HTTPException(409, {"ok": False, "reason": "lot_changed"})
        after = _shelf_stats(c)
        result = {"ok": True, "survivor_id": plan["survivor_id"],
                  "absorbed_ids": plan["absorbed_ids"], "total_qty": total,
                  "shelf_before": before, "shelf_after": after}
        try:
            c.execute(
                "INSERT INTO merges(survivor_id,absorbed_ids,total_qty,group_key,result_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (plan["survivor_id"], json.dumps(plan["absorbed_ids"]), total, key,
                 json.dumps(result), datetime.now(timezone.utc).isoformat()))
        except Exception:
            # 并发同组确认抢先落库：放弃本次写入，回放已存在的结果。
            c.rollback()
            row = c.execute("SELECT result_json FROM merges WHERE group_key=?", (key,)).fetchone()
            return {**json.loads(row["result_json"]), "idempotent": True}
        c.commit()
    finally:
        c.close()
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, date.today().isoformat())
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    c.commit(); c.close(); return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
