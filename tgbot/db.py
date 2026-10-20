"""Минималистичная база: у каждого пользователя один текущий диалог."""
from typing import Optional

import aiosqlite

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, id);
"""

_db: Optional[aiosqlite.Connection] = None


def _now() -> int:
    import time
    return int(time.time())


async def init_db() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(_SCHEMA)
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _c() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("База не инициализирована")
    return _db


async def ensure_user(uid: int, username: Optional[str]) -> None:
    await _c().execute(
        "INSERT INTO users(id, username, created) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
        (uid, username, _now()),
    )
    await _c().commit()


async def add_message(uid: int, role: str, content: str) -> None:
    await _c().execute(
        "INSERT INTO messages(user_id, role, content, ts) VALUES(?,?,?,?)",
        (uid, role, content, _now()),
    )
    await _c().commit()


async def get_messages(uid: int) -> list:
    cur = await _c().execute(
        "SELECT * FROM messages WHERE user_id=? ORDER BY id ASC", (uid,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def delete_messages(uid: int, ids: list) -> None:
    if not ids:
        return
    q = ",".join("?" * len(ids))
    await _c().execute(f"DELETE FROM messages WHERE user_id=? AND id IN ({q})", [uid, *ids])
    await _c().commit()


async def clear_messages(uid: int) -> None:
    """Начать новый диалог: старая история стирается полностью."""
    await _c().execute("DELETE FROM messages WHERE user_id=?", (uid,))
    await _c().commit()
