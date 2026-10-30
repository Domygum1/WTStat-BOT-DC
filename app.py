"""WTStat local API. All persistent files live under this file's directory."""
from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager, closing
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "wtstat.sqlite3"
CONFIG = ROOT / "config.local.json"
REPLAYS = ROOT / "cache" / "replays"
PARSER = ROOT / "bin" / "wtstat_results.exe"
HASHES = ROOT / "vendor" / "wrpl-inspector" / "data" / "ecshashes.json"
FILES = "https://wt-game-replays.warthunder.com"
MAX_REPLAY_PARTS = 256
UA = "WTStat/1.0 (local personal replay viewer)"
LOCK = threading.Lock()
LAST_REQUEST = 0.0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    sync_bundled_wiki()
    yield


app = FastAPI(title="WTStat", lifespan=lifespan)


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS pages (query TEXT, page INTEGER, fetched REAL, data TEXT, PRIMARY KEY(query,page))")
    c.execute("CREATE TABLE IF NOT EXISTS matches (sid TEXT PRIMARY KEY, data TEXT, fetched REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS match_stats (sid TEXT PRIMARY KEY, data TEXT, fetched REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS player_names (uid TEXT PRIMARY KEY, name TEXT, updated REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS favorites (uid TEXT PRIMARY KEY, added REAL NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS recent_players (query TEXT PRIMARY KEY COLLATE NOCASE, searched REAL NOT NULL)")
    c.commit()
    return c


def sync_bundled_wiki() -> None:
    """Install the tracked Wiki snapshot into a local database after git pull."""
    path = ROOT / "data" / "wiki_vehicles.csv"
    if not path.is_file():
        return
    fields = ("vehicle_id", "name", "category", "vehicle_role", "research_country_code",
              "research_country", "operator_country", "operator_countries_json", "br_ab",
              "br_rb", "br_sb", "br_ground_rb", "br_ground_sb", "source_url", "updated_utc")
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not set(fields).issubset(reader.fieldnames or []):
            raise RuntimeError("随程序提供的 Wiki 载具 CSV 缺少必要列。")
        rows = list(reader)
    ids = [row["vehicle_id"] for row in rows]
    if len(rows) < 3000 or not all(ids) or len(set(ids)) != len(ids):
        raise RuntimeError("随程序提供的 Wiki 载具 CSV 不完整，数据库未更新。")
    with closing(db()) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS wiki_vehicles ("
                           "vehicle_id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, "
                           "vehicle_role TEXT, research_country_code TEXT, research_country TEXT, "
                           "operator_country TEXT, operator_countries_json TEXT, "
                           "br_ab REAL, br_rb REAL, br_sb REAL, br_ground_rb REAL, br_ground_sb REAL, "
                           "source_url TEXT NOT NULL, updated_utc TEXT NOT NULL)")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(wiki_vehicles)")}
        for field in ("vehicle_role", "operator_countries_json"):
            if field not in columns:
                connection.execute(f"ALTER TABLE wiki_vehicles ADD COLUMN {field} TEXT")
        connection.execute("DELETE FROM wiki_vehicles")
        connection.executemany(
            f"INSERT INTO wiki_vehicles ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            ([row[field] for field in fields] for row in rows),
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_wiki_vehicles_name ON wiki_vehicles(name)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_wiki_vehicles_category ON wiki_vehicles(category)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_wiki_vehicles_country ON wiki_vehicles(research_country_code)")
        connection.commit()
    wiki_catalog.cache_clear()


def cookie():
    if not CONFIG.exists():
        return None
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8")).get("identity_sid")
        return value.strip() if isinstance(value, str) and value.strip() else None
    except (ValueError, OSError):
        return None


def request(url, *, auth=False, stream=False, method="GET", payload=None, headers=None):
    global LAST_REQUEST
    jar = {"identity_sid": cookie()} if auth and cookie() else None
    if auth and not jar:
        raise HTTPException(401, "官网搜索需要 identity_sid。请按 README 创建 config.local.json。")
    with LOCK:
        wait = 0.5 - (time.monotonic() - LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        LAST_REQUEST = time.monotonic()
    for attempt in range(3):
        try:
            r = requests.request(method, url, json=payload, cookies=jar,
                                 headers={"User-Agent": UA, **(headers or {})}, timeout=(8, 45),
                                 stream=stream, allow_redirects=True)
        except requests.RequestException as e:
            if attempt == 2:
                raise HTTPException(504, f"请求超时或网络错误：{type(e).__name__}") from e
            time.sleep(attempt + 1)
            continue
        if r.status_code == 429:
            delay = min(int(r.headers.get("Retry-After", "3") or "3"), 10) if str(r.headers.get("Retry-After", "3")).isdigit() else 3
            r.close()
            if attempt == 2:
                raise HTTPException(429, "Gaijin 请求频率限制，请稍后重试。")
            time.sleep(delay)
            continue
        if r.status_code >= 500 and attempt < 2:
            r.close()
            time.sleep(attempt + 1)
            continue
        return r
    raise HTTPException(502, "Gaijin 服务暂时不可用。")


def normalize_sid(value):
    value = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{16}", value) and re.search("[a-f]", value):
        return value
    if re.fullmatch(r"\d{15,20}", value):
        n = int(value)
        if n < 2**64:
            return f"{n:016x}"
    if re.fullmatch(r"[0-9a-f]{16}", value):
        return value
    raise HTTPException(400, "Session ID 必须是十进制或 16 位十六进制。")


def replay_api_page(payload):
    r = request("https://warthunder.com/en/api/replay", auth=True, method="POST", payload=payload)
    try:
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"官网搜索返回 HTTP {r.status_code}。")
        if "application/json" not in r.headers.get("Content-Type", ""):
            raise HTTPException(401, "官网未返回 Replay 数据；请更新 config.local.json 中的 identity_sid。")
        raw = r.json()
        if not isinstance(raw.get("items"), list) or not isinstance(raw.get("total_count"), int):
            raise HTTPException(502, "官网 Replay 接口结构已变化。")
        return raw
    finally:
        r.close()


def site_page(query, page, kind="USERNAME"):
    cache_key = f"api:{kind}:{query}"
    with closing(db()) as c:
        row = c.execute("SELECT * FROM pages WHERE query=? AND page=?", (cache_key, page)).fetchone()
        if row and time.time() - row["fetched"] < 300:
            cached = json.loads(row["data"])
            if cached.get("total") != 0 or cached.get("verified_empty"):
                return cached
    payload = {"gameMode": ["arcade", "realistic", "simulation"], "gameType": "randomBattle",
               "techType": "all", "findMissionValue": "", "findUserValue": query,
               "findUserType": kind, "isUserOwnReplays": False, "rankRange": "",
               "timeRangeFrom": "", "timeRangeTo": "", "limit": 15, "page": page}
    raw = replay_api_page(payload)
    if page == 1 and raw["total_count"] == 0:
        if not query or replay_api_page({**payload, "findUserValue": "",
                                         "findUserType": "USERNAME"})["total_count"] == 0:
            raise HTTPException(502, "官网无筛选回放列表也返回 0 场；identity_sid 可能已过期或官网暂时异常。请重新登录官网并更新 config.local.json 后重试。")
    items = []
    for entry in raw["items"]:
        try:
            sid = normalize_sid(str(entry["sessionId"]))
        except (KeyError, HTTPException):
            continue
        timestamp = entry.get("startTime")
        when = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if isinstance(timestamp, (int, float)) else None
        duration = (entry.get("endTime") or 0) - timestamp if isinstance(timestamp, (int, float)) else None
        items.append({"sid": sid, "title": entry.get("title"), "date_text": when,
                      "map": entry.get("missionName"), "game_type": entry.get("gameType"),
                      "game_mode": entry.get("gameMode"), "vehicles": entry.get("statisticGroup"),
                      "duration": f"{duration // 60} min" if duration and duration > 0 else None})
    result = {"items": items, "total": raw["total_count"]}
    if page == 1 and result["total"] == 0:
        result["verified_empty"] = True
    with closing(db()) as c:
        c.execute("REPLACE INTO pages VALUES (?,?,?,?)", (cache_key, page, time.time(), json.dumps(result)))
        c.commit()
    return result


def date_of(text):
    if not text:
        return None
    for pattern in (r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})"):
        m = re.search(pattern, text)
        if m:
            parts = m.groups()
            try:
                if len(parts[0]) == 4:
                    year, month, day = map(int, parts)
                elif "/" in m.group():  # English site: month/day/year
                    month, day, year = map(int, parts)
                else:
                    day, month, year = map(int, parts)
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        for match in re.finditer(r"(?:\d{1,2} [A-Za-z]+ \d{4}|[A-Za-z]+ \d{1,2},? \d{4})", text):
            try:
                return datetime.strptime(match.group(), fmt).date().isoformat()
            except ValueError:
                pass
    return None


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/stats")
def stats_page():
    return FileResponse(ROOT / "static" / "stats.html", headers={"Cache-Control": "no-store"})


@app.get("/vehicles")
def vehicle_table():
    return FileResponse(ROOT / "static" / "vehicles.html", headers={"Cache-Control": "no-store"})


@app.get("/fonts/wt-symbols.ttf")
def wt_symbols_font():
    return FileResponse(ROOT / "static" / "fonts" / "symbols_skyquake.bfc9728c6a6bc73f209a.ttf",
                        media_type="font/ttf", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/flags/{code}.svg")
def country_flag(code: str):
    if code not in {"us", "de", "su", "gb", "jp", "it", "fr", "se", "cn", "il"}:
        raise HTTPException(404, "国旗图标不存在。")
    return FileResponse(ROOT / "static" / "flags" / f"{code}.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/vehicles.csv")
def vehicle_csv():
    path = ROOT / "data" / "wiki_vehicles.csv"
    if not path.exists():
        raise HTTPException(503, "载具表尚未生成。请运行 scripts/update_wiki_vehicles.py。")
    return FileResponse(path, filename="wiki_vehicles.csv", media_type="text/csv")


def vehicle_normal_key(value):
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def vehicle_search_key(value):
    key = vehicle_normal_key(value)
    aliases = [key]
    # F/A-18A/C/E/F is also commonly searched as F18A/C/E/F.
    if "fa18" in key:
        aliases.append(key.replace("fa18", "f18"))
    return " ".join(aliases)


@app.get("/api/vehicles")
def vehicles(q: str = "", category: str = "", country: str = "", vehicle_id: str = "",
             offset: int = Query(0, ge=0, le=10000), limit: int = Query(100, ge=1, le=200),
             sort: str = "", direction: str = "desc"):
    clauses = []
    params = []
    if vehicle_id:
        clauses.append("vehicle_id = ? COLLATE NOCASE")
        params.append(vehicle_id[:100])
    if q.strip():
        raw = q.strip()[:100]
        term = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{term}%"
        normalized = vehicle_normal_key(raw)
        search = "(vehicle_id LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR operator_country LIKE ? ESCAPE '\\'"
        params.extend([term] * 3)
        if normalized:
            search += " OR vehicle_search_key(vehicle_id) LIKE ? OR vehicle_search_key(name) LIKE ?"
            params.extend([f"%{normalized}%"] * 2)
        clauses.append(search + ")")
    if category:
        clauses.append("category = ?")
        params.append(category)
    if country:
        clauses.append("research_country_code = ?")
        params.append(country)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    br_columns = {"br_ab", "br_rb", "br_sb", "br_ground_rb", "br_ground_sb"}
    if sort and sort not in br_columns:
        raise HTTPException(400, "不支持的权重排序字段。")
    if direction not in {"asc", "desc"}:
        raise HTTPException(400, "排序方向必须是 asc 或 desc。")
    order = (f"{sort} IS NULL, {sort} {direction.upper()}, category,research_country,name,vehicle_id"
             if sort else "category,research_country,name,vehicle_id")
    with db() as connection:
        connection.create_function("vehicle_search_key", 1, vehicle_search_key, deterministic=True)
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_vehicles'").fetchone():
            raise HTTPException(503, "载具表尚未生成。请运行 scripts/update_wiki_vehicles.py。")
        total = connection.execute("SELECT COUNT(*) FROM wiki_vehicles" + where, params).fetchone()[0]
        rows = connection.execute(
            "SELECT vehicle_id,name,category,vehicle_role,research_country_code,research_country,operator_country,"
            "br_ab,br_rb,br_sb,br_ground_rb,br_ground_sb,source_url "
            "FROM wiki_vehicles" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


@app.get("/api/status")
def status():
    return {"cookie_configured": bool(cookie()), "parser_ready": PARSER.exists()}


@app.get("/api/favorites")
def list_favorites():
    with closing(db()) as c:
        rows = c.execute("SELECT f.uid, f.uid AS query, p.name FROM favorites f LEFT JOIN player_names p ON p.uid = f.uid "
                         "ORDER BY f.added DESC, f.uid").fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/recent-players")
def list_recent_players():
    with closing(db()) as c:
        rows = c.execute("SELECT r.query, p.name, EXISTS(SELECT 1 FROM favorites f WHERE f.uid = r.query) AS followed "
                         "FROM recent_players r LEFT JOIN player_names p ON p.uid = r.query "
                         "ORDER BY r.searched DESC, r.query LIMIT 5").fetchall()
    return {"items": [dict(row) for row in rows]}


def record_recent_player(query: str):
    if len(query) > 64:
        return
    with closing(db()) as c:
        c.execute("REPLACE INTO recent_players (query, searched) VALUES (?, ?)", (query, time.time()))
        c.execute("DELETE FROM recent_players WHERE query NOT IN "
                  "(SELECT query FROM recent_players ORDER BY searched DESC, query LIMIT 5)")
        c.commit()


@app.put("/api/favorites/{uid}")
def add_favorite(uid: str):
    uid = uid.strip()
    if not uid or len(uid) > 64 or re.fullmatch(r"[0-9a-fA-F]{16}|\d{15,20}", uid):
        raise HTTPException(400, "请输入玩家昵称或 UID，不要输入 Session ID。")
    with closing(db()) as c:
        c.execute("INSERT OR IGNORE INTO favorites (uid, added) VALUES (?, ?)", (uid, time.time()))
        c.commit()
    return {"uid": uid}


@app.delete("/api/favorites/{uid}")
def remove_favorite(uid: str):
    with closing(db()) as c:
        c.execute("DELETE FROM favorites WHERE uid = ?", (uid,))
        c.commit()
    return {"uid": uid}


def cached_match_statuses(sids):
    sids = list(sids)
    if not sids:
        return {}
    placeholders = ",".join("?" for _ in sids)
    with closing(db()) as c:
        details = {row["sid"] for row in c.execute(
            f"SELECT sid FROM matches WHERE sid IN ({placeholders})", sids)}
        stats = {row["sid"] for row in c.execute(
            f"SELECT sid FROM match_stats WHERE sid IN ({placeholders})", sids)}
    return {sid: {"detail": sid in details, "stats": sid in stats,
                  "replay": any((REPLAYS / sid).glob("*.wrpl"))} for sid in sids}


@app.get("/api/cache")
def list_match_cache():
    """List every saved match, including stats-only and replay-only sessions."""
    items = {}
    with closing(db()) as connection:
        for table, flag in (("matches", "detail"), ("match_stats", "stats")):
            for row in connection.execute(f"SELECT sid, data, fetched FROM {table}"):
                sid = row["sid"]
                item = items.setdefault(sid, {"sid": sid, "detail": False,
                                              "stats": False, "replay_parts": 0,
                                              "expected_parts": None,
                                              "start_utc": None, "map": None,
                                              "game_mode": None, "battle_group": None,
                                              "fetched": 0})
                item[flag] = True
                item["fetched"] = max(item["fetched"], row["fetched"] or 0)
                try:
                    data = json.loads(row["data"])
                except (TypeError, ValueError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                if flag == "detail":
                    count = data.get("parts")
                    if isinstance(count, int) and not isinstance(count, bool) and 0 < count <= MAX_REPLAY_PARTS:
                        item["expected_parts"] = count
                for key in ("start_utc", "map", "game_mode", "battle_group"):
                    if not item[key] and data.get(key):
                        item[key] = data[key]
    if REPLAYS.is_dir():
        for folder in REPLAYS.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not re.fullmatch(r"[0-9a-f]{16}", folder.name):
                continue
            part_numbers = {int(path.stem) for path in folder.glob("*.wrpl")
                            if re.fullmatch(r"\d{4}", path.stem)
                            and path.is_file() and path.stat().st_size > 1234}
            if not part_numbers:
                continue
            item = items.setdefault(folder.name, {"sid": folder.name,
                                                  "detail": False, "stats": False,
                                                  "replay_parts": 0, "expected_parts": None,
                                                  "start_utc": None,
                                                  "map": None, "game_mode": None,
                                                  "battle_group": None, "fetched": 0})
            item["replay_parts"] = len(part_numbers)
            item["_part_numbers"] = part_numbers
    ordered = sorted(items.values(), key=lambda item: (
        item["start_utc"] or "", item["fetched"], item["sid"]), reverse=True)
    for item in ordered:
        item.pop("fetched")
        expected = item["expected_parts"]
        numbers = item.pop("_part_numbers", set())
        item["cache_level"] = ("complete" if item["detail"] and expected
                               and numbers == set(range(expected)) else "partial")
    return {"items": ordered, "total": len(ordered)}


@app.delete("/api/cache/selected")
def delete_selected_match_cache(sessions: list[str] = Body(embed=True)):
    if not sessions or len(sessions) > 5000:
        raise HTTPException(400, "请选择 1–5000 场对局。")
    sids = list(dict.fromkeys(normalize_sid(value) for value in sessions))
    base = REPLAYS.resolve()
    for sid in sids:
        folder = REPLAYS / sid
        if folder.is_symlink() or folder.resolve().parent != base:
            raise HTTPException(400, "缓存路径无效。")
    deleted, failed = [], []
    for sid in sids:
        try:
            delete_match_cache(sid)
            deleted.append(sid)
        except HTTPException as error:
            failed.append({"sid": sid, "reason": error.detail})
    return {"deleted": deleted, "failed": failed}


@app.delete("/api/cache/{session}")
def delete_match_cache(session: str):
    sid = normalize_sid(session)
    base = REPLAYS.resolve()
    folder = REPLAYS / sid
    if folder.is_symlink() or folder.resolve().parent != base:
        raise HTTPException(400, "缓存路径无效。")
    try:
        if folder.is_dir():
            shutil.rmtree(folder)
    except OSError as error:
        raise HTTPException(409, "缓存文件正在使用或无法删除，请稍后重试。") from error
    with closing(db()) as c:
        c.execute("DELETE FROM matches WHERE sid = ?", (sid,))
        c.execute("DELETE FROM match_stats WHERE sid = ?", (sid,))
        c.commit()
    return {"sid": sid, "cached": False}


@app.delete("/api/cache")
def clear_all_match_cache():
    cache_root = ROOT.resolve() / "cache"
    if ((ROOT / "cache").is_symlink() or REPLAYS.is_symlink()
            or REPLAYS.resolve() != cache_root / "replays"):
        raise HTTPException(400, "Replay 缓存路径无效。")
    replay_sessions = replay_parts = 0
    if REPLAYS.exists():
        if not REPLAYS.is_dir():
            raise HTTPException(409, "Replay 缓存路径不是目录。")
        replay_sessions = sum(path.is_dir() for path in REPLAYS.iterdir())
        replay_parts = sum(1 for _ in REPLAYS.rglob("*.wrpl"))
        try:
            shutil.rmtree(REPLAYS)
        except OSError as error:
            raise HTTPException(409, "Replay 文件正在使用或无法删除，请稍后重试。") from error
    REPLAYS.mkdir(parents=True, exist_ok=True)
    with closing(db()) as connection:
        matches = connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        stats = connection.execute("SELECT COUNT(*) FROM match_stats").fetchone()[0]
        connection.execute("DELETE FROM matches")
        connection.execute("DELETE FROM match_stats")
        connection.commit()
    return {"replay_sessions": replay_sessions, "replay_parts": replay_parts,
            "matches": matches, "match_stats": stats}


@app.delete("/api/replay-files")
def delete_replay_files(sessions: list[str] = Body(embed=True)):
    if not sessions or len(sessions) > 256:
        raise HTTPException(400, "请选择 1–256 场对局。")
    sids = list(dict.fromkeys(normalize_sid(value) for value in sessions))
    base = REPLAYS.resolve()
    deleted_parts = 0
    deleted_sessions = 0
    for sid in sids:
        folder = REPLAYS / sid
        if folder.is_symlink() or folder.resolve().parent != base:
            raise HTTPException(400, "Replay 缓存路径无效。")
        if not folder.is_dir():
            continue
        parts = len(list(folder.glob("*.wrpl")))
        try:
            shutil.rmtree(folder)
        except OSError as error:
            raise HTTPException(409, f"{sid} 的 Replay 文件正在使用或无法删除。") from error
        deleted_parts += parts
        deleted_sessions += 1
    return {"deleted_sessions": deleted_sessions, "deleted_parts": deleted_parts,
            "retained_stats": True}


@app.get("/api/search")
def search(q: str = Query(min_length=1), offset: int = Query(0, ge=0, le=10000),
           start: str | None = None, end: str | None = None):
    q = q.strip()
    if re.fullmatch(r"[0-9a-fA-F]{16}|\d{15,20}", q):
        sid = normalize_sid(q)
        cache = cached_match_statuses([sid])[sid]
        return {"items": [{"sid": sid, "title": "指定 Session", "date_text": None,
                           "map": None, "cached": cache["detail"] or cache["replay"],
                           "cache": cache}],
                "next_offset": None, "mode": "session"}
    kind = "ID" if q.isdigit() else "USERNAME"
    official_page = offset // 15 + 1
    result = site_page(q, official_page, kind)
    if offset == 0 and result["total"] > 0:
        record_recent_player(q)
    block = result["items"]
    selected = []
    for item in block:
        d = date_of(item["date_text"])
        if (start or end) and (not d or (start and d < start) or (end and d > end)):
            continue
        selected.append(item)
    statuses = cached_match_statuses(item["sid"] for item in selected)
    for item in selected:
        item["cache"] = statuses[item["sid"]]
        item["cached"] = item["cache"]["detail"] or item["cache"]["replay"]
    more = offset + len(block) < result["total"]
    return {"items": selected, "next_offset": offset + 15 if more else None, "mode": kind.lower(), "query": q,
            "total": result["total"], "note": "官网时间按 UTC 显示；日期范围筛选当前增量读取的页面。"}


def download(sid):
    folder = REPLAYS / sid
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for part in range(MAX_REPLAY_PARTS):
        path = folder / f"{part:04d}.wrpl"
        if path.exists() and path.stat().st_size > 1234:
            files.append(path)
            continue
        r = request(f"{FILES}/{sid}/{part:04d}.wrpl", stream=True)
        try:
            if r.status_code == 404:
                if part == 0:
                    raise HTTPException(404, "Replay 不存在或已过期（第 0 段 404）。")
                return files
            if r.status_code == 403:
                raise HTTPException(403, "Replay 下载被拒绝（403）。")
            if r.status_code != 200:
                raise HTTPException(502, f"下载第 {part} 段返回 HTTP {r.status_code}。")
            if int(r.headers.get("Content-Length", "0")) > 100_000_000:
                raise HTTPException(502, "Replay 分段超过 100 MB 上限。")
            temp = path.with_suffix(".part")
            size = 0
            try:
                with temp.open("wb") as f:
                    for chunk in r.iter_content(131072):
                        size += len(chunk)
                        if size > 100_000_000:
                            raise HTTPException(502, "Replay 分段超过 100 MB 上限。")
                        f.write(chunk)
                if size < 1234:
                    raise HTTPException(502, "Replay 分段不完整。")
                temp.replace(path)
            except Exception:
                temp.unlink(missing_ok=True)
                raise
            files.append(path)
        except requests.RequestException as e:
            raise HTTPException(504, f"下载第 {part} 段失败：{type(e).__name__}") from e
        finally:
            r.close()
    raise HTTPException(502, f"Replay 超过 {MAX_REPLAY_PARTS} 段安全上限。")


def download_result_part(sid):
    """Find the final contiguous segment with one-byte HTTP ranges, then download it."""
    folder = REPLAYS / sid
    folder.mkdir(parents=True, exist_ok=True)

    def exists(part):
        response = request(f"{FILES}/{sid}/{part:04d}.wrpl", stream=True,
                           headers={"Range": "bytes=0-0"})
        try:
            if response.status_code == 206 and response.headers.get("Content-Range", "").startswith("bytes 0-0/"):
                return True
            if response.status_code == 404:
                return False
            if response.status_code == 403:
                raise HTTPException(403, "Replay 下载被拒绝（403）。")
            raise HTTPException(502, f"Replay 分段探测返回 HTTP {response.status_code}；服务器可能不支持 Range。")
        finally:
            response.close()

    if not exists(0):
        raise HTTPException(404, "Replay 不存在或已过期（第 0 段 404）。")
    low, high = 0, 1
    while high < MAX_REPLAY_PARTS and exists(high):
        low, high = high, high * 2
    if high >= MAX_REPLAY_PARTS and exists(MAX_REPLAY_PARTS):
        raise HTTPException(502, f"Replay 可能超过 {MAX_REPLAY_PARTS} 段安全上限。")
    while high - low > 1:
        middle = (low + high) // 2
        if exists(middle):
            low = middle
        else:
            high = middle
    path = folder / f"{low:04d}.wrpl"
    if path.exists() and path.stat().st_size > 1234:
        return path
    response = request(f"{FILES}/{sid}/{low:04d}.wrpl", stream=True)
    try:
        if response.status_code != 200:
            raise HTTPException(502, f"下载结算分段返回 HTTP {response.status_code}。")
        temp = path.with_suffix(".part")
        size = 0
        try:
            with temp.open("wb") as target:
                for chunk in response.iter_content(131072):
                    size += len(chunk)
                    if size > 100_000_000:
                        raise HTTPException(502, "Replay 分段超过 100 MB 上限。")
                    target.write(chunk)
            if size < 1234:
                raise HTTPException(502, "Replay 结算分段不完整。")
            temp.replace(path)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    except requests.RequestException as error:
        raise HTTPException(504, "结算分段下载失败。") from error
    finally:
        response.close()
    return path


COUNTRIES = {"usa": "美国", "germany": "德国", "ussr": "苏联", "britain": "英国", "japan": "日本",
             "italy": "意大利", "france": "法国", "sweden": "瑞典", "china": "中国", "israel": "以色列"}

# Newer vehicle IDs missing from WT-Plotter's bundled catalog. Names and research
# countries are verified at https://wiki.warthunder.com/unit/<vehicle ID>.
VEHICLE_OVERRIDES = {
    "f_14d": ("F-14D", "usa"),
    "f_14d_vf_11": ("F-14D (VF-11)", "usa"),
    "f_16am_block_20_mlu_netherlands": ("F-16AM", "france"),
    "f_16xl": ("F-16XL", "usa"),
    "germ_boxer_swatrinf": ("sWaTrgInf", "germany"),
    "germ_schutzenpanzer_puma_vjtf_mod": ("Puma u14", "germany"),
    "mig_29_9_12": ("MiG-29 (9-12)", "ussr"),
    "mig_29kr_9_41r": ("MiG-29KR (9-41R)", "ussr"),
    "mig_29m_9_15": ("MiG-29M (9-15)", "ussr"),
    "saab_jas39d": ("JAS39D", "sweden"),
    "ussr_t_72b3_arena": ('T-72B3 "Arena"', "ussr"),
    "yak_130_early": ("Yak-130", "ussr"),
}


@lru_cache(maxsize=1)
def catalog():
    base = ROOT / "vendor" / "WT-Plotter" / "resources" / "translations"
    try:
        vehicles = json.loads((base / "vehicles.json").read_text(encoding="utf-8"))
        locations = json.loads((base / "locations.json").read_text(encoding="utf-8"))
        return ({x["identifier"]: x for x in vehicles}, {x["identifier"]: x for x in locations})
    except (OSError, ValueError):
        return {}, {}


@lru_cache(maxsize=1)
def wiki_catalog():
    with closing(db()) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_vehicles'").fetchone():
            return {}
        return {row["vehicle_id"].casefold(): dict(row) for row in connection.execute(
            "SELECT vehicle_id,name,research_country_code,research_country,operator_country,"
            "category,br_ab,br_rb,br_sb,br_ground_rb,br_ground_sb FROM wiki_vehicles"
        )}


def vehicle(code):
    wiki = wiki_catalog().get(code.casefold())
    if wiki:
        return {"name": wiki["name"], "code": code,
                "country": COUNTRIES.get(wiki["research_country_code"]) or wiki["research_country"],
                "research_country": wiki["research_country"],
                "operator_country": wiki["operator_country"]}
    if code in VEHICLE_OVERRIDES:
        name, country = VEHICLE_OVERRIDES[code]
        return {"name": name, "code": code, "country": COUNTRIES[country]}
    item = catalog()[0].get(code) or {}
    return {"name": item.get("en") or code, "code": code, "country": COUNTRIES.get(item.get("country"))}


def replay_kill_events(carved, players):
    names = {str(p["uid"]): p.get("name") for p in players}
    raw_log = carved.get("battle_log")
    if isinstance(raw_log, list):
        events = []
        for item in raw_log:
            if item.get("event_type") != "kill":
                continue
            killer_uid = item.get("attacker_uid") if item.get("attacker_uid") in names else None
            victim_uid = item.get("target_uid") if item.get("target_uid") in names else None
            owner_uid = item.get("weapon_owner_uid") if item.get("weapon_owner_uid") in names else None
            if victim_uid:
                target_name = names[victim_uid]
            elif item.get("target_kind") == "weapon_or_component":
                owner = names.get(owner_uid)
                target_name = f"武器／部件（持有者：{owner}）" if owner else "武器／部件"
            elif item.get("target_model"):
                target_name = item["target_model"]
            elif item.get("target_entity_uid") is not None:
                target_name = f"实体 #{item['target_entity_uid']}"
            else:
                target_name = "未知目标"
            attacker_name = (names.get(killer_uid) or item.get("attacker_model")
                             or (f"实体 #{item['attacker_entity_uid']}" if item.get("attacker_entity_uid") is not None else "未知来源"))
            events.append({
                "time_ms": item.get("time_ms"), "killer_uid": killer_uid,
                "killer_name": attacker_name, "victim_uid": victim_uid,
                "victim_name": target_name, "target_kind": item.get("target_kind"),
                "target_model": item.get("target_model"), "target_entity_uid": item.get("target_entity_uid"),
                "weapon_owner_uid": owner_uid, "weapon": item.get("weapon"),
            })
        return events
    events = []
    for kill in carved.get("kills") or []:
        killer_uid = str(kill.get("KillerID") or "")
        victim_uid = str(kill.get("VictimID") or "")
        if killer_uid not in names:
            killer_uid = None
        if victim_uid not in names:
            victim_uid = None
        events.append({
            "time_ms": kill.get("Time"),
            "killer_uid": killer_uid,
            "killer_name": names.get(killer_uid),
            "victim_uid": victim_uid,
            "victim_name": names.get(victim_uid),
            "weapon": kill.get("Weapon") or None,
        })
    return events


@lru_cache(maxsize=1)
def missile_names():
    path = ROOT / "data" / "missile_names.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        return {row["weapon_code"]: row for row in csv.DictReader(source)}


CANNON_AMMO_CODES = {
    "ap_i", "ap_i_t", "ap_i_ball", "he_frag_i", "he_frag_i_t", "sap_hei",
}


def add_missile_names(data):
    names = missile_names()
    for field in ("battle_log", "kill_events"):
        for event in data.get(field) or []:
            code = event.get("weapon")
            if code in CANNON_AMMO_CODES:
                event["weapon_name"] = "机炮"
                event["weapon_wiki_url"] = None
                continue
            matched = names.get(code)
            if matched:
                event["weapon_name"] = matched["name"]
                event["weapon_wiki_url"] = matched["wiki_url"] or None
    return data


def add_event_vehicle_names(data):
    resolved = {}
    for field in ("battle_log", "kill_events"):
        for event in data.get(field) or []:
            for side in ("attacker", "target"):
                model = event.get(f"{side}_model")
                if not model:
                    continue
                code = model.removeprefix("tankModels/")
                if code not in resolved:
                    name = vehicle(code)["name"]
                    resolved[code] = name if name != code else None
                event[f"{side}_vehicle_name"] = resolved[code]
    return data


def carve_local_replay(files, players):
    carved = subprocess.run(
        [str(PARSER), "--carve", str(files[0].parent), str(HASHES)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, check=True,
    )
    data = json.loads(carved.stdout)
    if not isinstance(data.get("battle_log"), list):
        raise ValueError("Replay 解析器未返回战斗日志，请重新构建 Go 程序")
    spawned = data.get("spawned") or {}
    for player in players:
        player["spawned"] = [vehicle(code) for code in sorted(spawned.get(player["uid"], {}))]
    return replay_kill_events(data, players), data.get("battle_log"), data.get("errors") or []


ACTIVITY_FIELDS = ("kills", "groundKills", "humanKills", "deaths", "assists", "score",
                   "aiKills", "aiGroundKills", "aiNavalKills", "captureZone", "teamKills")


def has_activity(player):
    return any(player.get(field) not in (None, 0, 0.0) for field in ACTIVITY_FIELDS)


def is_unjoined_player(player):
    return (player.get("team") not in (1, 2) and not player.get("configured")
            and not player.get("spawned") and not has_activity(player))


def has_participation_evidence(player):
    return bool(player.get("configured") or player.get("spawned") or has_activity(player))


def is_real_player(player):
    uid = str(player.get("uid") or "")
    name = player.get("name")
    return (uid.isascii() and uid.isdecimal() and int(uid) > 0
            and not (isinstance(name, str) and name.lower().startswith("coop/bot")))


def recover_saved_lineups(sid, players, files):
    """Use earlier results snapshots when final matchingInfo omits an active player."""
    missing = {str(player["uid"]): player for player in players
               if is_real_player(player) and has_activity(player) and not player.get("configured")}
    changed = False
    earlier_files = files[:-1]
    ordered = [path for path in earlier_files if path.name == "0001.wrpl"]
    ordered.extend(path for path in reversed(earlier_files) if path.name != "0001.wrpl")
    for earlier in ordered:
        if not missing:
            break
        try:
            snapshot = json.loads(subprocess.run(
                [str(PARSER), str(earlier)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=40, check=True).stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if (snapshot.get("header") or {}).get("session_id") != sid:
            continue
        matching = ((snapshot.get("results") or {}).get("matchingInfo") or {})
        for uid, player in tuple(missing.items()):
            crafts = (matching.get(uid) or {}).get("crafts_info") or {}
            configured = [vehicle(craft["name"]) for key, craft in sorted(
                crafts.items(), key=lambda item: int(item[0][5:])
                if re.fullmatch(r"array\d+", item[0]) else 9999)
                          if isinstance(craft, dict) and craft.get("name")]
            if configured:
                player["configured"] = configured
                missing.pop(uid)
                changed = True
    return changed


def missing_active_lineups(players):
    return any(is_real_player(player) and has_activity(player)
               and not player.get("configured") for player in players)


def download_early_part(sid):
    """Fetch 0001 only when a result snapshot needs a missing lineup."""
    path = REPLAYS / sid / "0001.wrpl"
    if path.exists() and path.stat().st_size > 1234:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    response = request(f"{FILES}/{sid}/0001.wrpl", stream=True)
    try:
        if response.status_code != 200:
            raise HTTPException(response.status_code, "Replay 第 0001 段不可用。")
        temp = path.with_suffix(".part")
        size = 0
        try:
            with temp.open("wb") as target:
                for chunk in response.iter_content(131072):
                    size += len(chunk)
                    if size > 100_000_000:
                        raise HTTPException(502, "Replay 分段超过 100 MB 上限。")
                    target.write(chunk)
            if size < 1234:
                raise HTTPException(502, "Replay 第 0001 段不完整。")
            temp.replace(path)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    finally:
        response.close()
    return path


def recover_missing_lineups(sid, players):
    """Try the early result, then all segments if active humans still lack lineups."""
    if not missing_active_lineups(players):
        return False
    files = sorted((REPLAYS / sid).glob("*.wrpl"))
    changed = len(files) > 1 and recover_saved_lineups(sid, players, files)
    if not missing_active_lineups(players):
        return changed
    if not any(path.name == "0001.wrpl" for path in files):
        try:
            download_early_part(sid)
            files = sorted((REPLAYS / sid).glob("*.wrpl"))
            changed = recover_saved_lineups(sid, players, files) or changed
        except (HTTPException, requests.RequestException):
            pass
    if missing_active_lineups(players):
        try:
            files = download(sid)
            changed = recover_saved_lineups(sid, players, files) or changed
        except (HTTPException, requests.RequestException):
            pass
    return changed


def parse_replay(sid, files, *, include_log=True, persist=True):
    if not PARSER.exists():
        raise HTTPException(500, "Go 解析器尚未构建。请运行 start.bat。")
    try:
        p = subprocess.run([str(PARSER), str(files[0]), str(files[-1])], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40, check=True)
        raw = json.loads(p.stdout)
    except (subprocess.SubprocessError, ValueError) as e:
        raise HTTPException(502, f"Replay 结算数据解析失败：{type(e).__name__}") from e
    result = raw.get("results") or {}
    header = raw.get("header") or {}
    players = []
    matching = result.get("matchingInfo") or {}
    for p in result.get("player") or []:
        uid = str(p.get("userId") or "")
        if not uid or uid == "-1":
            continue
        configured = []
        crafts = (matching.get(uid) or {}).get("crafts_info") or {}
        for key in sorted(crafts, key=lambda x: int(x[5:]) if re.fullmatch(r"array\d+", x) else 9999):
            item = crafts[key]
            if isinstance(item, dict) and item.get("name"):
                configured.append(vehicle(item["name"]))
        players.append({"uid": uid, "name": p.get("name"), "team": p.get("team"),
                        "squad_id": p.get("squadId"), "auto_squad": p.get("autoSquad"),
                        "spawned": [], "configured": configured,
                        "kills": p.get("kills"), "groundKills": p.get("groundKills"), "humanKills": p.get("humanKills"),
                        "deaths": p.get("deaths"), "assists": p.get("assists"), "score": p.get("score"),
                        "aiKills": p.get("aiKills"), "aiGroundKills": p.get("aiGroundKills"),
                        "aiNavalKills": p.get("aiNavalKills"), "captureZone": p.get("captureZone"),
                        "teamKills": p.get("teamKills")})
    players = [player for player in players if not is_unjoined_player(player)]
    recover_saved_lineups(sid, players, files)
    # The parser's entity reconstruction is version sensitive. Only publish a vehicle
    # as actually spawned if it is tied to the player's UID by an observed entity.
    kill_events, battle_log, battle_errors = None, None, None
    if include_log and files and HASHES.exists():
        try:
            kill_events, battle_log, battle_errors = carve_local_replay(files, players)
        except (subprocess.SubprocessError, ValueError):
            pass
    start = header.get("start_time")
    level = (header.get("level") or "").split("\0", 1)[0] or None
    map_key = Path(level).stem if level else None
    map_name = (catalog()[1].get(map_key) or {}).get("en") if map_key else None
    data = {"sid": sid, "start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat() if start else None,
            "map": map_name or (header.get("location") or "").split("\0", 1)[0] or None,
            "level": level, "game_mode": header.get("game_mode"),
            "battle_group": header.get("battle_group"),
            "players": players, "source": "Replay 结算 BLK", "parts": len(files),
            "vehicle_note": "配置载具是玩家带入对局的阵容，不能证明每辆都实际出击。"}
    if kill_events is not None:
        data["kill_events"] = kill_events
        data["battle_log"] = battle_log
        data["battle_log_errors"] = battle_errors
        data["battle_log_version"] = 4
    if persist:
        with db() as c:
            c.execute("REPLACE INTO matches VALUES (?,?,?)", (sid, json.dumps(data, ensure_ascii=False), time.time()))
            c.executemany("REPLACE INTO player_names VALUES (?,?,?)", [(p["uid"], p["name"], time.time()) for p in players if p["name"]])
    return data


def complete_cached_ai(sid, data, connection):
    """Add AI categories omitted by older WTStat versions, using local Replay files only."""
    players = data.get("players") or []
    if not players or all("aiGroundKills" in p and "aiNavalKills" in p for p in players):
        return data
    files = sorted((REPLAYS / sid).glob("*.wrpl"))
    if not files or not PARSER.exists():
        return data
    try:
        result = subprocess.run([str(PARSER), str(files[0]), str(files[-1])], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=40, check=True)
        raw_players = (json.loads(result.stdout).get("results") or {}).get("player") or []
    except (subprocess.SubprocessError, ValueError, OSError):
        return data
    by_uid = {str(p.get("userId")): p for p in raw_players if p.get("userId") is not None}
    changed = False
    for player in players:
        raw = by_uid.get(str(player.get("uid")))
        if raw is None:
            continue
        for key in ("aiGroundKills", "aiNavalKills"):
            if key not in player:
                player[key] = raw.get(key)
                changed = True
    if changed:
        connection.execute("UPDATE matches SET data=? WHERE sid=?", (json.dumps(data, ensure_ascii=False), sid))
    return data


def complete_cached_squads(sid, data, connection, table):
    """Backfill squad fields from the local results segment in older cache rows."""
    players = data.get("players") or []
    if not players or all("squad_id" in player and "auto_squad" in player for player in players):
        return data
    files = sorted((REPLAYS / sid).glob("*.wrpl"))
    if not files or not PARSER.exists():
        return data
    try:
        output = subprocess.run([str(PARSER), str(files[-1])], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=40, check=True)
        raw_players = (json.loads(output.stdout).get("results") or {}).get("player") or []
    except (subprocess.SubprocessError, ValueError, OSError):
        return data
    by_uid = {str(p.get("userId")): p for p in raw_players if p.get("userId") is not None}
    changed = False
    for player in players:
        raw = by_uid.get(str(player.get("uid")))
        if raw is None:
            continue
        for source, target in (("squadId", "squad_id"), ("autoSquad", "auto_squad")):
            if target not in player and source in raw:
                player[target] = raw[source]
                changed = True
    if changed:
        if table not in {"matches", "match_stats"}:
            raise ValueError("invalid cache table")
        connection.execute(f"UPDATE {table} SET data=? WHERE sid=?",
                           (json.dumps(data, ensure_ascii=False), sid))
    return data


def vehicle_battle_rating(code, mode, group):
    """Use the Wiki BR for this match; aircraft have separate ground RB/SB BRs."""
    if mode not in {"arcade", "realistic", "simulation"}:
        return None
    if group not in {"aircraft", "aviation", "air", "tank", "ground", "ground_vehicles",
                     "ship", "ships", "fleet", "naval", "boat"}:
        return None
    item = wiki_catalog().get(str(code).casefold())
    if not item:
        return None
    column = {"arcade": "br_ab", "realistic": "br_rb", "simulation": "br_sb"}[mode]
    if item["category"] == "aviation" and group in {"tank", "ground", "ground_vehicles"}:
        column = {"realistic": "br_ground_rb", "simulation": "br_ground_sb"}.get(mode, column)
    return item[column]


VEHICLE_CATEGORY_ORDER = {"ground": 0, "aviation": 1, "helicopters": 2,
                          "boats": 3, "ships": 3}


def vehicle_lineup_sort_key(item, mode, group):
    code = item.get("code") or ""
    wiki = wiki_catalog().get(str(code).casefold()) or {}
    rating = vehicle_battle_rating(code, mode, group)
    return (VEHICLE_CATEGORY_ORDER.get(wiki.get("category"), 4),
            rating is None, -rating if rating is not None else 0)


def local_replay_metadata(sid):
    """Recover mode and vehicle group from a retained WRPL part's header."""
    files = sorted((REPLAYS / sid).glob("*.wrpl"))
    if not files or not PARSER.exists():
        return None, None
    try:
        completed = subprocess.run([str(PARSER), str(files[0])], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=40, check=True)
        header = json.loads(completed.stdout).get("header") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    mode = header.get("game_mode")
    group = header.get("battle_group")
    return (mode if mode in {"arcade", "realistic", "simulation"} else None,
            group if group in {"aircraft", "tank", "ship"} else None)


def add_battle_ratings(sid, data, connection):
    """Resolve match mode lazily, then calculate each configured lineup's exact max BR."""
    mode, group = data.get("game_mode"), data.get("battle_group")
    players = data.get("players") or []
    data["players"] = [player for player in players if not is_unjoined_player(player)]
    changed = len(data["players"]) != len(players)
    if recover_missing_lineups(sid, data["players"]):
        changed = True
    if not (mode and group):
        for row in connection.execute("SELECT data FROM pages WHERE query LIKE 'api:%' ORDER BY fetched DESC"):
            try:
                found = next((item for item in json.loads(row["data"]).get("items", [])
                              if item.get("sid") == sid), None)
            except (TypeError, ValueError):
                continue
            if found:
                mode = mode or found.get("game_mode")
                group = group or found.get("vehicles")
                break
    if not (mode and group):
        local_mode, local_group = local_replay_metadata(sid)
        mode = mode or local_mode
        group = group or local_group
    if not (mode and group) and cookie() and time.time() - data.get("metadata_checked_at", 0) > 86400:
        try:
            response = request(f"https://warthunder.com/en/api/replay/{int(sid, 16)}", auth=True)
            try:
                if response.status_code == 200 and "application/json" in response.headers.get("Content-Type", ""):
                    official = response.json().get("replay")
                    if isinstance(official, dict):
                        mode = mode or official.get("gameMode")
                        group = group or official.get("statisticGroup")
                    data["metadata_checked_at"] = time.time()
                    changed = True
            finally:
                response.close()
        except (HTTPException, ValueError):
            pass
    if mode != data.get("game_mode") or group != data.get("battle_group"):
        data["game_mode"], data["battle_group"] = mode, group
        changed = True
    if changed:
        payload = json.dumps(data, ensure_ascii=False)
        cursor = connection.execute("UPDATE matches SET data=? WHERE sid=?", (payload, sid))
        if not cursor.rowcount:
            connection.execute("UPDATE match_stats SET data=? WHERE sid=?", (payload, sid))
    for player in data.get("players", []):
        configured = player.get("configured") or []
        configured.sort(key=lambda item: vehicle_lineup_sort_key(item, mode, group))
        ratings = [vehicle_battle_rating(item.get("code"), mode, group) for item in configured]
        player["max_br"] = max(ratings) if ratings and all(value is not None for value in ratings) else None
    players = data.get("players") or []
    participants = [player for player in players
                    if is_real_player(player) and has_participation_evidence(player)]
    ratings = [player.get("max_br") for player in participants]
    data["match_br"] = (max(ratings) if ratings and all(isinstance(br, (int, float)) for br in ratings)
                        else None)
    for player in players:
        player["squad_br"] = None
        player["squad_members"] = None
        if not is_real_player(player) or not has_participation_evidence(player):
            continue
        if player.get("auto_squad") is True:
            members = [player]
        elif player.get("auto_squad") is False:
            squad_id = player.get("squad_id")
            team = player.get("team")
            members = ([other for other in participants
                        if other.get("team") == team and other.get("squad_id") == squad_id
                        and other.get("auto_squad") is False]
                       if squad_id is not None and team is not None else [player])
        else:
            continue
        member_ratings = [member.get("max_br") for member in members]
        if member_ratings and all(isinstance(br, (int, float)) for br in member_ratings):
            player["squad_br"] = max(member_ratings)
        player["squad_members"] = [member.get("name") or member.get("uid") for member in members]
    return data


@app.get("/api/detail/{session}")
def detail(session: str):
    sid = normalize_sid(session)
    with db() as c:
        row = c.execute("SELECT data FROM matches WHERE sid=?", (sid,)).fetchone()
        if row:
            data = complete_cached_ai(sid, json.loads(row["data"]), c)
            data = complete_cached_squads(sid, data, c, "matches")
            if (data.get("battle_log_version") != 4 or "battle_log" not in data) and PARSER.exists() and HASHES.exists():
                cached_files = sorted((REPLAYS / sid).glob("*.wrpl"))
                if cached_files:
                    try:
                        data["kill_events"], data["battle_log"], data["battle_log_errors"] = carve_local_replay(cached_files, data.get("players", []))
                        data["battle_log_version"] = 4
                        c.execute("UPDATE matches SET data=? WHERE sid=?", (json.dumps(data, ensure_ascii=False), sid))
                    except (subprocess.SubprocessError, ValueError):
                        pass
            data["vehicle_note"] = "配置载具是玩家带入对局的阵容，不能证明每辆都实际出击。"
            for player in data.get("players", []):
                for field in ("configured", "spawned"):
                    for item in player.get(field, []):
                        if item.get("code"):
                            item.update(vehicle(item["code"]))
            return add_event_vehicle_names(add_missile_names(add_battle_ratings(sid, data, c)))
    files = download(sid)
    data = parse_replay(sid, files)
    with db() as c:
        return add_event_vehicle_names(add_missile_names(add_battle_ratings(sid, data, c)))


@app.get("/api/stats/{session}")
def match_statistics(session: str):
    sid = normalize_sid(session)
    with db() as connection:
        row = connection.execute("SELECT data FROM matches WHERE sid=?", (sid,)).fetchone()
        if not row:
            row = connection.execute("SELECT data FROM match_stats WHERE sid=?", (sid,)).fetchone()
        data = json.loads(row["data"]) if row else None
        if data is not None:
            data = complete_cached_squads(sid, data, connection,
                                          "matches" if connection.execute(
                                              "SELECT 1 FROM matches WHERE sid=?", (sid,)).fetchone()
                                          else "match_stats")
    if data is None:
        local_parts = sorted((REPLAYS / sid).glob("*.wrpl"))
        if local_parts:
            data = parse_replay(sid, [local_parts[-1]], include_log=False, persist=False)
        if not data or not data["players"]:
            result_part = download_result_part(sid)
            data = parse_replay(sid, [result_part], include_log=False, persist=False)
        if not data["players"]:
            raise HTTPException(502, "Replay 末段没有可读取的玩家结算数据。")
    with db() as connection:
        data = add_battle_ratings(sid, data, connection)
        if not data.get("game_mode") or not data.get("battle_group"):
            raise HTTPException(422, "无法确认对局模式，不能选择正确的 Wiki 权重列。")
        if not row:
            connection.execute("REPLACE INTO match_stats VALUES (?,?,?)",
                               (sid, json.dumps(data, ensure_ascii=False), time.time()))
    players = data.get("players") or []
    match_br = data.get("match_br")
    summary = []
    for player in players:
        configured = player.get("configured") or []
        countries = {v.get("country") for v in configured}
        summary.append({"uid": player.get("uid"), "name": player.get("name"),
                        "team": player.get("team"),
                        "max_br": player.get("max_br"), "squad_br": player.get("squad_br"),
                        "squad_id": player.get("squad_id"), "auto_squad": player.get("auto_squad"),
                        "squad_members": player.get("squad_members"),
                        "lineup_country": next(iter(countries)) if configured and len(countries) == 1 else None,
                        "configured_vehicles": [{"code": v.get("code"), "name": v.get("name")}
                                                for v in configured],
                        "stats": {field: player.get(field) for field in
                                  ("kills", "groundKills", "humanKills", "deaths", "assists", "score",
                                   "aiKills", "aiGroundKills", "aiNavalKills",
                                   "captureZone", "teamKills")}})
    for player in summary:
        if player["auto_squad"] is True:
            player["squad_countries"] = []
            player["party_key"] = f"solo:{player['uid']}"
        elif player["auto_squad"] is False and player["team"] is not None and player["squad_id"] is not None:
            mates = [other for other in summary if other["uid"] != player["uid"]
                     and other["team"] == player["team"] and other["squad_id"] == player["squad_id"]
                     and other["auto_squad"] is False]
            player["squad_countries"] = [mate["lineup_country"] for mate in sorted(
                mates, key=lambda mate: str(mate["uid"]))]
            player["party_key"] = f"squad:{player['team']}:{player['squad_id']}"
        else:
            player["squad_countries"] = None
            player["party_key"] = None
    return {"sid": sid, "game_mode": data["game_mode"], "battle_group": data["battle_group"],
            "match_br": match_br, "start_utc": data.get("start_utc"), "map": data.get("map"),
            "players": summary,
            "br_complete": match_br is not None, "source": "Replay 结算 BLK"}
