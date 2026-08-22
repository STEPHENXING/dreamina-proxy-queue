"""database.py — SQLite 数据库操作层。

所有的表操作集中在这里。使用 WAL 模式，支持多线程读写。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

_local = threading.local()
_db_path: Optional[str] = None

# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------


def init_db(data_dir: str, filename: str = "db.sqlite"):
    """初始化数据库：设置路径、建表。"""
    global _db_path
    os.makedirs(data_dir, exist_ok=True)
    _db_path = os.path.join(data_dir, filename)
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "tasks", "generation_mode", "TEXT NOT NULL DEFAULT 'multimodal'")
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（per-thread）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def transaction():
    """简单的事务上下文管理器。"""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    password    TEXT    NOT NULL,
    role        TEXT    NOT NULL DEFAULT 'customer',
    credits     REAL    NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    cookie_path TEXT,
    CHECK (role IN ('customer', 'provider', 'admin'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT    NOT NULL UNIQUE,
    customer        TEXT    NOT NULL,
    provider        TEXT,
    prompt          TEXT    NOT NULL,
    model_version   TEXT    NOT NULL,
    generation_mode TEXT    NOT NULL DEFAULT 'multimodal',
    duration        INTEGER NOT NULL,
    ratio           TEXT    NOT NULL,
    is_queued       INTEGER NOT NULL DEFAULT 0,
    credits         REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'queued',
    dreamina_task_id TEXT,
    result_url      TEXT,
    error           TEXT,
    progress        TEXT,
    progress_meta   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    submitted_at    TEXT,
    completed_at    TEXT,
    failed_at       TEXT,
    CHECK (status IN ('queued','submitting','dreamina_processing',
                      'completed','failed','rejected','cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_customer ON tasks(customer);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_provider ON tasks(provider);

CREATE TABLE IF NOT EXISTS media (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id        TEXT    NOT NULL UNIQUE,
    customer        TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    original_name   TEXT    NOT NULL,
    file_path       TEXT    NOT NULL,
    thumb_path      TEXT,
    url             TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (kind IN ('image', 'audio'))
);
CREATE INDEX IF NOT EXISTS idx_media_customer ON media(customer);

CREATE TABLE IF NOT EXISTS task_media (
    task_id     TEXT NOT NULL,
    media_id    TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, media_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user        TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    credits     REAL    NOT NULL,
    task_id     TEXT,
    note        TEXT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (type IN ('deduct','refund','admin_set','admin_adjust','provider_reward'))
);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user);
"""

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def create_user(username: str, password_hash: str, role: str = "customer",
                credits: float = 0, cookie_path: str = None) -> Dict:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO users (username, password, role, credits, cookie_path) VALUES (?,?,?,?,?)",
        (username, password_hash, role, credits, cookie_path),
    )
    conn.commit()
    return get_user(username)


def get_user(username: str) -> Optional[Dict]:
    row = _get_conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def get_all_users() -> List[Dict]:
    rows = _get_conn().execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_users_by_role(role: str) -> List[Dict]:
    rows = _get_conn().execute("SELECT * FROM users WHERE role=? ORDER BY id", (role,)).fetchall()
    return [dict(r) for r in rows]


def update_user_credits(username: str, credits: float):
    _get_conn().execute("UPDATE users SET credits=? WHERE username=?", (credits, username))
    _get_conn().commit()


def delete_user(username: str):
    _get_conn().execute("DELETE FROM users WHERE username=?", (username,))
    _get_conn().commit()


def update_user_cookie_path(username: str, cookie_path: str):
    _get_conn().execute("UPDATE users SET cookie_path=? WHERE username=?", (cookie_path, username))
    _get_conn().commit()


def adjust_credits(username: str, delta: float) -> float:
    """原子增减积分，返回新余额。"""
    conn = _get_conn()
    conn.execute("UPDATE users SET credits = credits + ? WHERE username=?", (delta, username))
    conn.commit()
    row = conn.execute("SELECT credits FROM users WHERE username=?", (username,)).fetchone()
    return row["credits"] if row else 0


def set_credits(username: str, value: float):
    _get_conn().execute("UPDATE users SET credits=? WHERE username=?", (value, username))
    _get_conn().commit()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def create_task(task_id: str, customer: str, prompt: str, model_version: str,
                duration: int, ratio: str, is_queued: int, credits: float,
                status: str = "queued", generation_mode: str = "multimodal") -> Dict:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO tasks (task_id, customer, prompt, model_version, generation_mode,
           duration, ratio, is_queued, credits, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (task_id, customer, prompt, model_version, generation_mode, duration, ratio,
         is_queued, credits, status),
    )
    conn.commit()
    return get_task(task_id)


def get_task(task_id: str) -> Optional[Dict]:
    row = _get_conn().execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def update_task(task_id: str, **fields):
    """更新任务的指定字段。"""
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    _get_conn().execute(f"UPDATE tasks SET {sets} WHERE task_id=?", vals)
    _get_conn().commit()


def get_tasks_by_customer(customer: str, limit: int = 200) -> List[Dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tasks WHERE customer=? ORDER BY created_at DESC LIMIT ?",
        (customer, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_tasks_by_status(status: str) -> List[Dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tasks WHERE status=? ORDER BY created_at", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_tasks_by_provider_and_status(provider: str, status: str) -> List[Dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tasks WHERE provider=? AND status=? ORDER BY created_at",
        (provider, status),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_tasks_by_customer(customer: str) -> List[Dict]:
    """获取用户处于活跃状态的任务 (queued / submitting / dreamina_processing)。"""
    rows = _get_conn().execute(
        """SELECT * FROM tasks WHERE customer=?
           AND status IN ('queued','submitting','dreamina_processing')
           ORDER BY created_at""",
        (customer,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_tasks(limit: int = 500) -> List[Dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_task(task_id: str):
    conn = _get_conn()
    conn.execute("DELETE FROM task_media WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def create_media(media_id: str, customer: str, kind: str, original_name: str,
                 file_path: str, thumb_path: str, url: str) -> Dict:
    conn = _get_conn()
    conn.execute(
        """INSERT INTO media (media_id, customer, kind, original_name,
           file_path, thumb_path, url) VALUES (?,?,?,?,?,?,?)""",
        (media_id, customer, kind, original_name, file_path, thumb_path, url),
    )
    conn.commit()
    return get_media(media_id)


def get_media(media_id: str) -> Optional[Dict]:
    row = _get_conn().execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
    return dict(row) if row else None


def get_media_by_customer(customer: str) -> List[Dict]:
    rows = _get_conn().execute(
        "SELECT * FROM media WHERE customer=? ORDER BY created_at DESC", (customer,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Task-Media 关联
# ---------------------------------------------------------------------------


def link_task_media(task_id: str, media_ids: list):
    conn = _get_conn()
    for pos, mid in enumerate(media_ids):
        conn.execute(
            "INSERT OR IGNORE INTO task_media (task_id, media_id, position) VALUES (?,?,?)",
            (task_id, mid, pos),
        )
    conn.commit()


def get_task_media(task_id: str) -> List[Dict]:
    rows = _get_conn().execute(
        """SELECT m.*, tm.position FROM media m
           JOIN task_media tm ON m.media_id = tm.media_id
           WHERE tm.task_id=? ORDER BY tm.position""",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def add_transaction(user: str, tx_type: str, credits: float,
                    task_id: str = None, note: str = None):
    _get_conn().execute(
        "INSERT INTO transactions (user, type, credits, task_id, note) VALUES (?,?,?,?,?)",
        (user, tx_type, credits, task_id, note),
    )
    _get_conn().commit()


def get_transactions(user: str = None, limit: int = 200) -> List[Dict]:
    if user:
        rows = _get_conn().execute(
            "SELECT * FROM transactions WHERE user=? ORDER BY timestamp DESC LIMIT ?",
            (user, limit),
        ).fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM transactions ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
