import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    # 写事务并发时后到者等待先到者提交（BEGIN IMMEDIATE 拿到锁后再读，
    # 保证合并确认与消费互相收口，而不是各自读到旧快照）。
    c.execute("PRAGMA busy_timeout=5000")
    return c
