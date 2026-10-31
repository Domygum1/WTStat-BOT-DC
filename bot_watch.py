"""Durable, channel-scoped watches for newly listed WTStat matches."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4


WATCH_INTERVAL = 15


def parse_duration(hours: float) -> int:
    """Convert a numeric number of hours to seconds (for example, 0.5 -> 30 minutes)."""
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours):
        raise ValueError("监听时间请只填小时数，例如 1 或 0.5。")
    seconds = round(hours * 3600)
    if not 60 <= seconds <= 24 * 3600:
        raise ValueError("监听时间须在 1 分钟至 24 小时之间，只填数字。")
    return seconds


@dataclass(frozen=True)
class WatchJob:
    id: str
    channel_id: int
    player: str
    expires_at: float
    known: list[str]
    pending: list[str]


def merge_scan(job: WatchJob, current_sids: list[str]) -> WatchJob:
    """Queue unseen matches oldest first, retaining pending work across restarts."""
    current = list(dict.fromkeys(current_sids))
    known = set(job.known)
    pending = [*job.pending, *[sid for sid in reversed(current) if sid not in known and sid not in job.pending]]
    return replace(job, known=list(dict.fromkeys([*current, *job.known]))[:200], pending=pending)


class WatchStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS bot_watches ("
                               "channel_id INTEGER PRIMARY KEY, id TEXT NOT NULL, player TEXT NOT NULL, "
                               "expires_at REAL NOT NULL, known TEXT NOT NULL, pending TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _decode(row):
        return (WatchJob(row["id"], row["channel_id"], row["player"], row["expires_at"],
                         json.loads(row["known"]), json.loads(row["pending"])) if row else None)

    def replace(self, channel_id: int, player: str, duration: int, baseline: list[str]) -> WatchJob:
        job = WatchJob(uuid4().hex, channel_id, player, time.time() + duration,
                       list(dict.fromkeys(baseline))[:200], [])
        with self._connect() as connection:
            connection.execute("REPLACE INTO bot_watches VALUES (?,?,?,?,?,?)",
                               (job.channel_id, job.id, job.player, job.expires_at,
                                json.dumps(job.known), json.dumps(job.pending)))
        return job

    def get(self, channel_id: int) -> WatchJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM bot_watches WHERE channel_id=?", (channel_id,)).fetchone()
        return self._decode(row)

    def list_active(self) -> list[WatchJob]:
        with self._connect() as connection:
            connection.execute("DELETE FROM bot_watches WHERE expires_at <= ?", (time.time(),))
            rows = connection.execute("SELECT * FROM bot_watches ORDER BY channel_id").fetchall()
        return [self._decode(row) for row in rows]

    def save(self, job: WatchJob) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE bot_watches SET known=?, pending=? WHERE channel_id=? AND id=?",
                                        (json.dumps(job.known), json.dumps(job.pending), job.channel_id, job.id))
            return cursor.rowcount == 1

    def delete(self, channel_id: int, job_id: str | None = None) -> bool:
        with self._connect() as connection:
            if job_id is None:
                cursor = connection.execute("DELETE FROM bot_watches WHERE channel_id=?", (channel_id,))
            else:
                cursor = connection.execute("DELETE FROM bot_watches WHERE channel_id=? AND id=?",
                                            (channel_id, job_id))
            return cursor.rowcount == 1
