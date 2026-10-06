"""合并 / FEFO 扣减的端到端回归。

钉死同一条业务线：
- 预览只报当前真实条数/余量，不按合并后谎报；
- 确认后旧批号收走、只留 survivor，随后扣减只能打到 survivor；
- 同组重复确认幂等，不倍增余量；
- 合并确认与在途扣减在写锁上互斥，锁内重读，不出现“余量没加上、行已删、
  履历记旧身份”的撕裂；
- 脏净门与扣减候选门同源：on_shelf 且正余量；脏净之间不得并批；
- 合并失败时总表条数、紧急条、收走名单全部回到合并前；
- 总表 /fridge 与分层 /fridge?layer= 同一份数据，确认后两页只见 survivor。
"""

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        c.dir = tmp_path
        yield c


def _db(client) -> sqlite3.Connection:
    c = sqlite3.connect(client.dir / "pantryfifo.db")
    c.row_factory = sqlite3.Row
    return c


def err_reason(r) -> str:
    # HTTPException(409, plan) → {"detail": {"ok": False, "reason": ...}}
    d = r.json()
    return d["detail"]["reason"] if isinstance(d.get("detail"), dict) else d.get("reason")


def add_lot(client, item_id: int, qty: float, expiry: str, quality: str = "clean") -> int:
    # 入库接口只收 clean；脏批直接落库，走同一张表。
    with _db(client) as c:
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, qty, qty, expiry, "on_shelf", quality),
        )
        return cur.lastrowid


def lot(client, lot_id: int) -> dict:
    with _db(client) as c:
        return dict(c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone())


def on_shelf_ids(client) -> set[int]:
    with _db(client) as c:
        return {r["id"] for r in c.execute("SELECT id FROM lots WHERE status='on_shelf'")}


def merge_count(client) -> int:
    with _db(client) as c:
        return c.execute("SELECT COUNT(*) n FROM merges").fetchone()["n"]


def consume_history_lot_ids(client) -> list[int]:
    with _db(client) as c:
        ids = []
        for r in c.execute("SELECT result_json FROM consumptions"):
            for d in json.loads(r["result_json"])["deductions"]:
                ids.append(d["lot_id"])
        return ids


def test_preview_reports_current_count_not_post_merge(client):
    client.post("/api/expire-sweep")  # 清掉种子里的过期批，避免噪音
    before = client.get("/api/fridge").json()
    a = add_lot(client, 1, 2, "2026-12-20")
    add_lot(client, 1, 3, "2026-12-20")

    r = client.post("/api/merge/preview", json={"lot_ids": [a, a + 1]})
    assert r.status_code == 200, r.text
    body = r.json()
    # 预览阶段库存未动：条数必须仍是当前真实条数（合并前），不能预先减 1。
    assert body["shelf_before"]["lot_count"] == len(before) + 2
    total_now = sum(x["qty_remain"] for x in client.get("/api/fridge").json())
    assert body["shelf_before"]["total_qty"] == pytest.approx(round(total_now, 6))
    # 回包不得携带“扣减偏好批号”：合并未确认前 FEFO 该打谁由真实在架批决定。
    assert "hit_ids" not in body
    # 预览没有任何副作用：没有 merges 落库、行状态不变。
    assert merge_count(client) == 0
    assert {a, a + 1} <= on_shelf_ids(client)


def test_confirm_then_two_pages_and_consume_align_on_survivor(client):
    client.post("/api/expire-sweep")
    a = add_lot(client, 1, 2, "2026-12-20")  # 同 id 更小 → survivor
    b = add_lot(client, 1, 3, "2026-12-20")

    pv = client.post("/api/merge/preview", json={"lot_ids": [a, b]}).json()
    assert pv["plan"]["survivor_id"] == a
    assert pv["plan"]["absorbed_ids"] == [b]

    r = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 200, r.text
    assert r.json()["survivor_id"] == a

    # 三处必须对上同一批：页面两行、收走列、扣减打的号。
    full = {x["id"]: x for x in client.get("/api/fridge").json()}
    upper = {x["id"]: x for x in client.get("/api/fridge?layer=upper").json()}
    assert b not in full and b not in upper          # 总表/分层都见不到旧身份
    assert full[a]["qty_remain"] == 5 and upper[a]["qty_remain"] == 5
    with _db(client) as c:
        taken = json.loads(c.execute(
            "SELECT absorbed_ids FROM merges WHERE survivor_id=?", (a,)).fetchone()["absorbed_ids"])
    assert taken == [b]
    assert lot(client, b)["status"] == "merged"

    cr = client.post("/api/consume", json={"item_id": 1, "qty": 4})
    assert cr.status_code == 200, cr.text
    deductions = cr.json()["deductions"]
    assert {d["lot_id"] for d in deductions} == {a}   # 扣减只打留下的身份
    assert sum(d["take"] for d in deductions) == 4
    assert lot(client, a)["qty_remain"] == 1
    assert consume_history_lot_ids(client) == [a]     # 履历也不能记旧身份


def test_duplicate_confirm_does_not_double_survivor_qty(client):
    client.post("/api/expire-sweep")
    a = add_lot(client, 1, 2, "2026-12-20")
    b = add_lot(client, 1, 3, "2026-12-20")
    first = client.post("/api/merge/confirm", json={"lot_ids": [a, b]}).json()
    assert first["total_qty"] == 5

    second = client.post("/api/merge/confirm", json={"lot_ids": [b, a]}).json()
    assert second.get("idempotent") is True
    assert lot(client, a)["qty_remain"] == 5           # 不是 10/8，余量不再加一遍
    assert lot(client, b)["status"] == "merged"
    assert merge_count(client) == 1
    assert len(client.get("/api/fridge").json()) == len(
        {x["id"] for x in client.get("/api/fridge").json()})  # 没有幽灵行


def test_consume_waiting_on_merge_lock_hits_survivor(client):
    """合并先拿写锁并在锁内完成并批；在途扣减等锁后按新状态扣 survivor。"""
    client.post("/api/expire-sweep")
    a = add_lot(client, 1, 10, "2026-12-20")
    b = add_lot(client, 1, 10, "2026-12-20")

    holder = _db(client)
    holder.execute("BEGIN IMMEDIATE")
    box = {}

    def consume():
        box["r"] = client.post("/api/consume", json={"item_id": 1, "qty": 12})

    t = threading.Thread(target=consume)
    t.start()
    threading.Event().wait(0.4)  # 等扣减堵在写锁上
    # 持锁方完成合并：survivor=a 余量 20，b 收走。
    holder.execute("UPDATE lots SET qty_remain=20 WHERE id=?", (a,))
    holder.execute("UPDATE lots SET status='merged',qty_remain=0 WHERE id=?", (b,))
    holder.commit()
    holder.close()
    t.join(timeout=10)

    r = box["r"]
    assert r.status_code == 200, r.text
    assert {d["lot_id"] for d in r.json()["deductions"]} == {a}
    assert lot(client, a)["qty_remain"] == 8
    assert consume_history_lot_ids(client) == [a]     # 不能记已吞掉的 b


def test_merge_waiting_on_consume_lock_keeps_qty_conserved(client):
    """扣减先拿锁并先提交；合并等锁后按扣减后的真实余量收口，不丢量、不翻倍。"""
    client.post("/api/expire-sweep")
    a = add_lot(client, 1, 10, "2026-12-20")
    b = add_lot(client, 1, 10, "2026-12-20")

    holder = _db(client)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE lots SET qty_remain=6 WHERE id=?", (a,))  # 在途扣 4，未提交
    box = {}

    def do_merge():
        box["r"] = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})

    t = threading.Thread(target=do_merge)
    t.start()
    threading.Event().wait(0.4)
    holder.commit()
    holder.close()
    t.join(timeout=10)

    r = box["r"]
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_qty"] == 16                    # 6+10，按锁内真实余量
    assert lot(client, a)["qty_remain"] == 16         # 留下的行余量确实加上了
    assert lot(client, b)["status"] == "merged"       # 旧行收走
    assert lot(client, b)["qty_remain"] == 0


def test_dirty_clean_share_single_eligibility_gate(client):
    client.post("/api/expire-sweep")
    # 脏 + 净：不得合成一条；失败无副作用。
    a = add_lot(client, 3, 4, "2026-12-30", "clean")
    b = add_lot(client, 3, 5, "2026-12-30", "dirty")
    before = {(x["id"], x["qty_remain"], x["status"]) for x in client.get("/api/fridge").json()}

    r = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 409
    assert err_reason(r) == "quality_mismatch"
    after = {(x["id"], x["qty_remain"], x["status"]) for x in client.get("/api/fridge").json()}
    assert before == after                            # 名单/余量/状态全回合并前
    assert merge_count(client) == 0

    # 同质量的两条脏批（最早到期）：能合并；脏批也在扣减候选里（同一套门：on_shelf + 正余量）。
    d1 = add_lot(client, 3, 4, "2026-12-20", "dirty")
    d2 = add_lot(client, 3, 5, "2026-12-20", "dirty")
    r = client.post("/api/merge/confirm", json={"lot_ids": [d1, d2]})
    assert r.status_code == 200, r.text
    assert r.json()["survivor_id"] == d1
    cr = client.post("/api/consume", json={"item_id": 3, "qty": 7})
    assert {d["lot_id"] for d in cr.json()["deductions"]} == {d1}
    assert lot(client, d1)["qty_remain"] == 2

    # 负余量批既进不了扣减候选，也进不了合并。
    neg = add_lot(client, 3, -2, "2026-12-20", "dirty")
    r = client.post("/api/merge/confirm", json={"lot_ids": [d1, neg]})
    assert r.status_code == 409
    assert err_reason(r) == "lot_not_mergeable"
    cr = client.post("/api/consume", json={"item_id": 3, "qty": 2})
    assert neg not in {d["lot_id"] for d in cr.json()["deductions"]}


def test_failed_merge_restores_count_alerts_and_taken_list(client):
    client.post("/api/expire-sweep")
    a = add_lot(client, 1, 4, "2026-12-20")
    b = add_lot(client, 1, 4, "2026-12-21")  # 异期
    before_rows = {x["id"]: x["qty_remain"] for x in client.get("/api/fridge").json()}
    before_alerts = {(x["id"], x["level"]) for x in client.get("/api/alerts").json()}

    r = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 409
    assert err_reason(r) == "expiry_mismatch"

    # 总表条数、紧急条、收走名单一起回到合并前。
    after_rows = {x["id"]: x["qty_remain"] for x in client.get("/api/fridge").json()}
    after_alerts = {(x["id"], x["level"]) for x in client.get("/api/alerts").json()}
    assert after_rows == before_rows
    assert after_alerts == before_alerts
    assert merge_count(client) == 0
    with _db(client) as c:
        assert c.execute(
            "SELECT COUNT(*) n FROM lots WHERE status='merged'").fetchone()["n"] == 0

    # 异品同样整单失败。
    c_id = add_lot(client, 2, 4, "2026-12-20")
    r = client.post("/api/merge/confirm", json={"lot_ids": [a, c_id]})
    assert r.status_code == 409 and err_reason(r) == "item_mismatch"
    assert merge_count(client) == 0
