# -*- coding: utf-8 -*-
"""历史记录的 SQLite 存储层（只碰数据库，不碰 tkinter）。

设计要点
--------
1. 一条记录 = 一次「生成提示词」：上传用的压缩 JPEG + 正向 / 负向提示词（英文、中文）
   + 提示词解读 + 当时的风格、描述对象、补充要求、模型、耗时、token 数。
2. 图片以 BLOB 存进同一个数据库文件，不额外散落文件；存的是**上传前压缩过的 JPEG**
   （长边 ≤ 1280），一次生成几十到几百 KB，和实际发给模型的字节一致。
3. 列表查询（`list_records`）**不读** blob，只有点开某一条时才用 `get_image` 按 id 取图，
   记录多了列表也不会卡。
4. 排序按 `created_ts` 倒序（同一秒再按 id 倒序）：**越晚生成的越靠前**。
5. 数据库文件默认放在本文件旁边（`history.db`），已在 .gitignore 里。
6. 另有一张 `settings` 键值表，存「上次选图的文件夹」这类小偏好，和记录共用一个库文件。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(APP_DIR, "history.db")

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts    REAL    NOT NULL,
    created_at    TEXT    NOT NULL,
    image_name    TEXT    NOT NULL DEFAULT '',
    image_mime    TEXT    NOT NULL DEFAULT 'image/jpeg',
    image_blob    BLOB,
    image_w       INTEGER NOT NULL DEFAULT 0,
    image_h       INTEGER NOT NULL DEFAULT 0,
    style         TEXT    NOT NULL DEFAULT '',
    subject       TEXT    NOT NULL DEFAULT '',
    extra         TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    positive_en   TEXT    NOT NULL DEFAULT '',
    negative_en   TEXT    NOT NULL DEFAULT '',
    positive_zh   TEXT    NOT NULL DEFAULT '',
    negative_zh   TEXT    NOT NULL DEFAULT '',
    tips_zh       TEXT    NOT NULL DEFAULT '',
    json_ok       INTEGER NOT NULL DEFAULT 0,
    elapsed       REAL    NOT NULL DEFAULT 0,
    tokens_total  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_created ON history (created_ts);

-- 小的键值配置（例如「上次选图的文件夹」），和历史记录共用一个库文件
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# 列表页要的列（故意不含 image_blob）
LIST_COLUMNS = ("id, created_at, image_name, image_w, image_h, style, subject, "
                "model, positive_en, json_ok, elapsed, tokens_total")
# 详情页要的列（同样不含 blob，图片单独取）
FULL_COLUMNS = ("id, created_ts, created_at, image_name, image_mime, image_w, image_h, "
                "style, subject, extra, model, positive_en, negative_en, positive_zh, "
                "negative_zh, tips_zh, json_ok, elapsed, tokens_total")

_lock = threading.RLock()
_initialized: set[str] = set()


def db_path(path: "str | None" = None) -> str:
    """实际使用的数据库路径；传 path 只为方便测试用临时库。"""
    return path or DB_FILE


def connect(path: "str | None" = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    # WAL：写的时候还能读，界面刷新不会和后台写入互相卡住
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _session(path: "str | None" = None):
    """一次操作一个连接；正常结束提交，出错回滚，最后一定关掉。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: "str | None" = None) -> str:
    """建表建索引（幂等），返回实际使用的数据库路径。"""
    target = db_path(path)
    with _lock:
        if target in _initialized and os.path.exists(target):
            return target
        with _session(target) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        _initialized.add(target)
    return target


def save_record(*, image_bytes: "bytes | None" = None, image_name: str = "",
                image_mime: str = "image/jpeg", image_w: int = 0, image_h: int = 0,
                style: str = "", subject: str = "", extra: str = "", model: str = "",
                positive_en: str = "", negative_en: str = "",
                positive_zh: str = "", negative_zh: str = "", tips_zh: str = "",
                json_ok: bool = False, elapsed: float = 0.0, tokens_total: int = 0,
                ts: "float | None" = None, path: "str | None" = None) -> int:
    """存一条历史记录，返回自增 id。"""
    target = init_db(path)
    stamp = time.time() if ts is None else float(ts)
    values = {
        "created_ts": stamp,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)),
        "image_name": image_name or "",
        "image_mime": image_mime or "image/jpeg",
        "image_blob": sqlite3.Binary(image_bytes) if image_bytes else None,
        "image_w": int(image_w or 0),
        "image_h": int(image_h or 0),
        "style": style or "",
        "subject": subject or "",
        "extra": extra or "",
        "model": model or "",
        "positive_en": positive_en or "",
        "negative_en": negative_en or "",
        "positive_zh": positive_zh or "",
        "negative_zh": negative_zh or "",
        "tips_zh": tips_zh or "",
        "json_ok": 1 if json_ok else 0,
        "elapsed": float(elapsed or 0.0),
        "tokens_total": int(tokens_total or 0),
    }
    sql = (f"INSERT INTO history ({', '.join(values)}) "
           f"VALUES ({', '.join(':' + k for k in values)})")
    with _lock, _session(target) as conn:
        cur = conn.execute(sql, values)
        return int(cur.lastrowid)


def list_records(limit: "int | None" = None,
                 path: "str | None" = None) -> list[dict]:
    """按生成时间倒序（最新在最前）返回记录，不含图片字节。"""
    target = init_db(path)
    sql = f"SELECT {LIST_COLUMNS} FROM history ORDER BY created_ts DESC, id DESC"
    args: tuple = ()
    if limit:
        sql += " LIMIT ?"
        args = (int(limit),)
    with _lock, _session(target) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_record(record_id: int, path: "str | None" = None) -> "dict | None":
    """按 id 取一条记录的完整字段（不含图片字节）。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        row = conn.execute(
            f"SELECT {FULL_COLUMNS} FROM history WHERE id = ?", (int(record_id),)
        ).fetchone()
    return dict(row) if row else None


def get_image(record_id: int, path: "str | None" = None) -> "tuple[bytes | None, str]":
    """按 id 取图片，返回 (bytes | None, mime)。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        row = conn.execute(
            "SELECT image_blob, image_mime FROM history WHERE id = ?", (int(record_id),)
        ).fetchone()
    if not row or row["image_blob"] is None:
        return None, "image/jpeg"
    return bytes(row["image_blob"]), row["image_mime"] or "image/jpeg"


def delete_record(record_id: int, path: "str | None" = None) -> bool:
    """删一条，返回是否真的删掉了。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        cur = conn.execute("DELETE FROM history WHERE id = ?", (int(record_id),))
        return cur.rowcount > 0


def clear_all(path: "str | None" = None) -> int:
    """清空历史，返回删掉的条数。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        cur = conn.execute("DELETE FROM history")
        return max(cur.rowcount, 0)


def stats(path: "str | None" = None) -> dict:
    """给界面显示的概况：条数、图片总字节、最早 / 最晚一条的时间。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(LENGTH(image_blob)), 0) AS bytes, "
            "MIN(created_at) AS oldest, MAX(created_at) AS newest FROM history"
        ).fetchone()
    return {"count": int(row["cnt"]), "bytes": int(row["bytes"]),
            "oldest": row["oldest"] or "", "newest": row["newest"] or "",
            "db_file": target}


# ------------------------------------------------------------------
#  小配置（键值对）：比如记住上次选图的文件夹、上次保存的目录
# ------------------------------------------------------------------
def get_setting(key: str, default: str = "", path: "str | None" = None) -> str:
    """读一项配置，没有就返回 default。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (str(key),)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, path: "str | None" = None) -> None:
    """写一项配置（有就覆盖）。"""
    target = init_db(path)
    with _lock, _session(target) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (str(key), str(value)))
