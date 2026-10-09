# copyright by berlonak
# telegram: @Kilax123
"""Хранилище на SQLite.

Три задачи:
  1. Идемпотентность — один и тот же Update не обрабатывается дважды
     (webhook доставляется повторно при сбоях, MAX повторяет до 10 раз).
  2. Архив — все сообщения и метаданные медиа.
  3. Mapping «оригинал -> копии» — защита от петли, replies, edit/delete.
     Одно исходное сообщение может дать НЕСКОЛЬКО копий (длинный текст,
     альбом 11-12 элементов), поэтому mapping хранит part_index.
"""
from __future__ import annotations

import hashlib
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_updates (
    dedup_key   TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    sender_id     TEXT,
    sender_name   TEXT,
    sender_login  TEXT,
    text          TEXT,
    ts            INTEGER,
    album_id      TEXT,
    is_channel    INTEGER DEFAULT 0,
    content_hash  TEXT,
    generated_by_bridge INTEGER DEFAULT 0,
    created_at    INTEGER NOT NULL,
    UNIQUE(platform, message_id)
);

CREATE TABLE IF NOT EXISTS media (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_row   INTEGER NOT NULL REFERENCES messages(id),
    kind          TEXT NOT NULL,
    file_name     TEXT,
    mime          TEXT,
    size          INTEGER,
    local_path    TEXT,
    sha256        TEXT,
    src_ref       TEXT
);

CREATE TABLE IF NOT EXISTS forward_map (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    src_platform   TEXT NOT NULL,
    src_message_id TEXT NOT NULL,
    dst_platform   TEXT NOT NULL,
    dst_message_id TEXT NOT NULL,
    part_index     INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    UNIQUE(src_platform, src_message_id, dst_platform, part_index)
);
CREATE INDEX IF NOT EXISTS idx_fwd_dst ON forward_map(dst_platform, dst_message_id);
CREATE INDEX IF NOT EXISTS idx_fwd_src ON forward_map(src_platform, src_message_id);
"""


class Db:
    def __init__(self, path: str):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ---------------- идемпотентность ----------------
    async def seen_update(self, dedup_key: str) -> bool:
        """True, если этот Update уже обрабатывался. Иначе помечает его как обработанный."""
        try:
            await self._db.execute(
                "INSERT INTO processed_updates (dedup_key, created_at) VALUES (?,?)",
                (dedup_key, int(time.time())),
            )
            await self._db.commit()
            return False
        except aiosqlite.IntegrityError:
            return True

    async def purge_old_updates(self, older_than_days: int = 7) -> None:
        cutoff = int(time.time()) - older_than_days * 86400
        await self._db.execute("DELETE FROM processed_updates WHERE created_at < ?", (cutoff,))
        await self._db.commit()

    # ---------------- архив ----------------
    async def save_message(self, msg, generated_by_bridge: bool = False) -> int:
        chash = content_hash(msg)
        cur = await self._db.execute(
            """INSERT OR IGNORE INTO messages
               (platform, chat_id, message_id, sender_id, sender_name, sender_login,
                text, ts, album_id, is_channel, content_hash, generated_by_bridge, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (msg.platform, str(msg.chat_id), str(msg.message_id), str(msg.sender_id),
             msg.sender_name, msg.sender_username, msg.text, msg.timestamp,
             msg.album_id, int(msg.is_channel_post), chash,
             int(generated_by_bridge), int(time.time())),
        )
        await self._db.commit()

        row_id = cur.lastrowid
        if not row_id:
            c2 = await self._db.execute(
                "SELECT id FROM messages WHERE platform=? AND message_id=?",
                (msg.platform, str(msg.message_id)))
            row = await c2.fetchone()
            row_id = row[0]

        for m in msg.media:
            await self._db.execute(
                """INSERT INTO media
                   (message_row, kind, file_name, mime, size, local_path, sha256, src_ref)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row_id, m.kind, m.file_name, m.mime, m.size, m.local_path,
                 _file_sha256(m.local_path), m.file_ref),
            )
        await self._db.commit()
        return row_id

    # ---------------- mapping ----------------
    async def is_bridged_copy(self, platform: str, message_id: str) -> bool:
        cur = await self._db.execute(
            "SELECT 1 FROM forward_map WHERE dst_platform=? AND dst_message_id=? LIMIT 1",
            (platform, str(message_id)))
        return (await cur.fetchone()) is not None

    async def map_forward(self, src_platform: str, src_message_id: str,
                          dst_platform: str, dst_message_id: str,
                          part_index: int = 0) -> None:
        await self._db.execute(
            """INSERT OR IGNORE INTO forward_map
               (src_platform, src_message_id, dst_platform, dst_message_id,
                part_index, created_at)
               VALUES (?,?,?,?,?,?)""",
            (src_platform, str(src_message_id), dst_platform, str(dst_message_id),
             part_index, int(time.time())))
        await self._db.commit()

    async def dst_message_for(self, src_platform: str, src_message_id: str,
                              dst_platform: str) -> str | None:
        """Первая (главная) копия — для восстановления reply-треда."""
        cur = await self._db.execute(
            """SELECT dst_message_id FROM forward_map
               WHERE src_platform=? AND src_message_id=? AND dst_platform=?
               ORDER BY part_index LIMIT 1""",
            (src_platform, str(src_message_id), dst_platform))
        row = await cur.fetchone()
        return row[0] if row else None

    async def all_dst_messages(self, src_platform: str, src_message_id: str,
                               dst_platform: str) -> list[str]:
        """Все копии — нужны для edit/delete составных сообщений."""
        cur = await self._db.execute(
            """SELECT dst_message_id FROM forward_map
               WHERE src_platform=? AND src_message_id=? AND dst_platform=?
               ORDER BY part_index""",
            (src_platform, str(src_message_id), dst_platform))
        return [r[0] for r in await cur.fetchall()]


def content_hash(msg) -> str:
    """Хэш содержимого. Только текста мало: двое могут прислать одинаковое."""
    h = hashlib.sha256()
    h.update(msg.platform.encode())
    h.update(str(msg.chat_id).encode())
    h.update(str(msg.message_id).encode())
    h.update((msg.text or "").encode())
    for m in msg.media:
        h.update((m.file_name or "").encode())
        h.update(str(m.size).encode())
    return h.hexdigest()


def _file_sha256(path: str | None) -> str | None:
    if not path:
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None
