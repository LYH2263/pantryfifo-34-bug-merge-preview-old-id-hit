"""同日合并全链路不变量测试。

对应口径：
- 预览只读、条数按合并前如实报；确认后旧批号被吞进留下的行。
- 三处数字对同一批：页上条数 / 扣减打的号 / 收走列。
- 同一对批第二次确认不倍增（幂等回放）。
- 合并确认与在途扣减叠上：最终状态与串行执行一致，履历记当前身份。
- 脏行/干净行能否并批，与能否进扣减候选同一套门。
- 合并失败：总表条数、紧急条、收走名单一起回到合并前。
"""

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.main import ConsumeIn, MergeIn, app, consume, merge_confirm

# 种子数据（见 app/seed.py）：
#   lot1 item1 ×2  2026-10-01 clean   lot2 item1 ×1  2026-09-28 clean
#   lot3 item2 ×12 2026-11-01 clean   lot4 item3 ×1  2025-01-01 dirty
#   lot5 item2 ×-3 2026-12-01 dirty（负余量，进不了扣减候选）


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    with TestClient(app) as c:
        yield c


def db_one(sql, args=()):
    c = connect()
    row = c.execute(sql, args).fetchone()
    c.close()
    return dict(row) if row else None


def db_all(sql, args=()):
    c = connect()
    rows = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return rows


def shelf_stats():
    r = db_one("SELECT COUNT(*) n, COALESCE(SUM(qty_remain),0) q FROM lots WHERE status='on_shelf'")
    return {"lot_count": r["n"], "total_qty": round(float(r["q"]), 6)}


def add_lot(client, item_id, qty, expiry):
    r = client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry})
    assert r.status_code == 200
    return r.json()["id"]


def set_quality(lid, quality):
    c = connect()
    c.execute("UPDATE lots SET data_quality=? WHERE id=?", (quality, lid))
    c.commit()
    c.close()


@pytest.fixture()
def pair(client):
    """在种子 lot3 旁再入一条同品同到期日的干净批，构成可合并对。"""
    lid = add_lot(client, 2, 3, "2026-11-01")
    return 3, lid  # survivor 必为 id 较小的 lot3


# --- 预览：只读、条数不动、hit 指向留下的身份 -------------------------------

def test_preview_is_readonly_and_reports_current_counts(client, pair):
    a, b = pair
    before = shelf_stats()
    r = client.post("/api/merge/preview", json={"lot_ids": [a, b]})
    assert r.status_code == 200
    body = r.json()
    # 页上条数：预览回包必须是合并前的真实条数，不得按合并后谎报。
    assert body["shelf_before"] == before
    # 扣减打的号 = 留下的 survivor；收走列 = absorbed_ids。
    assert body["plan"]["survivor_id"] == a
    assert body["plan"]["absorbed_ids"] == [b]
    assert body["hit_ids"] == [a]
    assert body["plan"]["total_qty"] == 15
    # 只读：库存与合并履历都不动。
    assert shelf_stats() == before
    assert db_one("SELECT qty_remain q, status s FROM lots WHERE id=?", (b,)) == {"q": 3, "s": "on_shelf"}
    assert db_one("SELECT COUNT(*) n FROM merges")["n"] == 0


# --- 确认：旧批号吞进留下的行，随后扣减打 survivor，履历记当前身份 ---------

def test_confirm_absorbs_and_consume_hits_survivor(client, pair):
    a, b = pair
    r = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 200
    body = r.json()
    assert body["survivor_id"] == a and body["absorbed_ids"] == [b]
    assert body["hit_ids"] == [a]
    # 条数 -1、余量合计不变，前后对得上。
    assert body["shelf_after"]["lot_count"] == body["shelf_before"]["lot_count"] - 1
    assert body["shelf_after"]["total_qty"] == body["shelf_before"]["total_qty"]
    # 旧身份离开总表，留下的行余量 = 合并前之和。
    survivor = db_one("SELECT qty_remain q, status s FROM lots WHERE id=?", (a,))
    assert survivor == {"q": 15, "s": "on_shelf"}
    assert db_one("SELECT status s, qty_remain q FROM lots WHERE id=?", (b,)) == {"q": 0, "s": "merged"}
    fridge_ids = {x["id"] for x in client.get("/api/fridge").json()}
    assert a in fridge_ids and b not in fridge_ids
    # 随后扣减打到留下的身份，履历记的也是它。
    r = client.post("/api/consume", json={"item_id": 2, "qty": 4})
    assert r.status_code == 200
    assert [d["lot_id"] for d in r.json()["deductions"]] == [a]
    hist = json.loads(db_one("SELECT result_json j FROM consumptions ORDER BY id DESC LIMIT 1")["j"])
    assert [d["lot_id"] for d in hist["deductions"]] == [a]
    assert db_one("SELECT qty_remain q FROM lots WHERE id=?", (a,))["q"] == 11


# --- 幂等：同一对批第二次确认不倍增 ---------------------------------------

def test_second_confirm_same_pair_is_idempotent(client, pair):
    a, b = pair
    r1 = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r1.status_code == 200
    r2 = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r2.status_code == 200
    assert r2.json().get("idempotent") is True
    # 留下的行余量不倍增，被吞行不重复扣。
    assert db_one("SELECT qty_remain q FROM lots WHERE id=?", (a,))["q"] == 15
    assert db_one("SELECT COUNT(*) n FROM merges")["n"] == 1


# --- 同一套门：脏净不可并批；候选资格与扣减一致 ----------------------------

def test_dirty_and_clean_cannot_merge(client, pair):
    a, b = pair
    set_quality(b, "dirty")
    before = shelf_stats()
    for endpoint in ("preview", "confirm"):
        r = client.post(f"/api/merge/{endpoint}", json={"lot_ids": [a, b]})
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "quality_mismatch"
    assert shelf_stats() == before  # 全部回到合并前


def test_dirty_pair_can_merge_when_consistent(client, pair):
    _, b = pair
    c_lot = add_lot(client, 2, 5, "2026-11-01")
    set_quality(b, "dirty")
    set_quality(c_lot, "dirty")
    r = client.post("/api/merge/confirm", json={"lot_ids": [b, c_lot]})
    assert r.status_code == 200
    assert r.json()["survivor_id"] == b
    assert db_one("SELECT qty_remain q FROM lots WHERE id=?", (b,))["q"] == 8


def test_non_candidate_cannot_merge(client, pair):
    a, b = pair
    # 负余量批（种子 lot5）与零余量批都进不了扣减候选，也进不了合并。
    c = connect()
    c.execute("UPDATE lots SET qty_remain=0 WHERE id=?", (b,))
    c.commit()
    c.close()
    r = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "lot_not_mergeable"
    r = client.post("/api/merge/confirm", json={"lot_ids": [a, 5]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] in ("lot_not_mergeable", "expiry_mismatch")


# --- 失败回滚：条数 / 紧急条 / 收走名单一起回到合并前 ----------------------

def test_failed_merge_rolls_back_everything(client, pair):
    a, _ = pair
    stats_before = shelf_stats()
    alerts_before = client.get("/api/alerts").json()
    fridge_before = client.get("/api/fridge").json()
    # 同品异到期日：整单失败。
    r = client.post("/api/merge/confirm", json={"lot_ids": [1, 2]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "expiry_mismatch"
    # 异品：整单失败。
    r = client.post("/api/merge/confirm", json={"lot_ids": [1, a]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "item_mismatch"
    # 总表条数、紧急条、收走名单全部保持合并前。
    assert shelf_stats() == stats_before
    assert client.get("/api/alerts").json() == alerts_before
    assert client.get("/api/fridge").json() == fridge_before
    assert db_one("SELECT COUNT(*) n FROM merges")["n"] == 0
    assert db_one("SELECT status s FROM lots WHERE id=1")["s"] == "on_shelf"
    assert db_one("SELECT status s FROM lots WHERE id=2")["s"] == "on_shelf"


# --- 并发：合并确认与在途扣减叠上，结果与串行一致 --------------------------

def test_merge_confirm_overlapping_inflight_consume(client, pair):
    a, b = pair
    barrier = threading.Barrier(2)
    errors = []

    def do_merge():
        try:
            barrier.wait()
            merge_confirm(MergeIn(lot_ids=[a, b]))
        except Exception as e:  # noqa: BLE001 - 收集后统一断言
            errors.append(e)

    def do_consume():
        try:
            barrier.wait()
            consume(ConsumeIn(item_id=2, qty=4, note="inflight"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1, t2 = threading.Thread(target=do_merge), threading.Thread(target=do_consume)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert errors == []
    # 两种串行顺序终态相同：留下的行 15-4=11，旧行已吞。
    assert db_one("SELECT qty_remain q, status s FROM lots WHERE id=?", (a,)) == {"q": 11, "s": "on_shelf"}
    assert db_one("SELECT status s, qty_remain q FROM lots WHERE id=?", (b,)) == {"q": 0, "s": "merged"}
    # 履历记的是当前身份（survivor），不是被吞掉的旧号。
    hist = json.loads(db_one("SELECT result_json j FROM consumptions ORDER BY id DESC LIMIT 1")["j"])
    assert [d["lot_id"] for d in hist["deductions"]] == [a]
    # 总表与收走名单一致：b 只在 merges 履历里，不在总表。
    assert b not in {x["id"] for x in client.get("/api/fridge").json()}
    merge_row = db_one("SELECT survivor_id s, absorbed_ids ab FROM merges")
    assert merge_row["s"] == a and json.loads(merge_row["ab"]) == [b]


# --- 从总表发起合并：对齐后总表只见留下的批号 ------------------------------

def test_fridge_shows_only_survivor_after_merge(client, pair):
    a, b = pair
    before_ids = {x["id"] for x in client.get("/api/fridge").json()}
    assert {a, b} <= before_ids
    r = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 200
    after = client.get("/api/fridge").json()
    after_ids = {x["id"] for x in after}
    assert a in after_ids and b not in after_ids
    survivor_row = next(x for x in after if x["id"] == a)
    assert survivor_row["qty_remain"] == 15
    # 紧急条与总表同口径：被吞行不再出现在预警里。
    assert b not in {x["id"] for x in client.get("/api/alerts").json()}


# --- 参数校验 ---------------------------------------------------------------

def test_preview_rejects_bad_selections(client, pair):
    a, _ = pair
    assert client.post("/api/merge/preview", json={"lot_ids": [a]}).status_code == 409
    r = client.post("/api/merge/preview", json={"lot_ids": [a, a]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "duplicate_lot"
    assert client.post("/api/merge/preview", json={"lot_ids": []}).status_code == 409
