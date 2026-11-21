#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网聊天室 · 服务器 v3（多房间 + 图片 + 头像，SQLite 存储）
=====================================================================
纯 Python 标准库，不需要安装任何第三方包。
数据库是 SQLite（chat.db），在线状态只放内存。

v3 新增：
  · 多房间（所有人可建、所有人可见），房间之间消息/已读/在线完全隔离
  · 发图片（客户端压缩后上传，存进数据库，单独端点读取并长缓存）
  · 自定义头像（按设备保存，带版本号，其他端自动刷新）
  · 界面重做成微信风（前端）

v2 已有：已读/未读、撤回、离线补发、失败重试、断线重连、在线成员（按设备去重）、
        消息编辑、未读数量、输入校验、历史分页。

启动：python server.py      （或双击 启动聊天室.bat）
停止：Ctrl + C
=====================================================================
"""

import base64
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 控制台输出遇到 GBK 表示不了的字符时用 ? 替代，避免因为一个表情崩掉
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------
# 可调参数（想改规则就改这里）
# ---------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = _env_int("CHAT_PORT", 9999)

MAX_KEEP = _env_int("CHAT_MAX_KEEP", 500)              # 每个房间保留多少条历史
RECALL_WINDOW = _env_int("CHAT_RECALL_WINDOW", 120)    # 撤回时限（秒）
EDIT_WINDOW = _env_int("CHAT_EDIT_WINDOW", 600)        # 编辑时限（秒）
MSG_MAX_LEN = _env_int("CHAT_MSG_MAX_LEN", 2000)       # 单条文字最长
NAME_MAX_LEN = _env_int("CHAT_NAME_MAX_LEN", 20)       # 昵称/房间名最长
RATE_LIMIT_COUNT = _env_int("CHAT_RATE_LIMIT", 5)      # 限流条数
RATE_LIMIT_WINDOW = _env_int("CHAT_RATE_WINDOW", 5)    # 限流窗口（秒）
ONLINE_TIMEOUT = _env_int("CHAT_ONLINE_TIMEOUT", 20)   # 心跳超时（秒）
HISTORY_PAGE = _env_int("CHAT_HISTORY_PAGE", 30)       # 历史每页条数

IMAGE_MAX_BYTES = _env_int("CHAT_IMAGE_MAX_BYTES", 1_500_000)   # 单张图片上限
AVATAR_MAX_BYTES = _env_int("CHAT_AVATAR_MAX_BYTES", 200_000)    # 头像上限
ROOM_NAME_MAX = 20

DB_FILE = Path(os.environ.get("CHAT_DB") or (BASE_DIR / "chat.db"))

# 旧版 data.json：只用于首次迁移
_legacy_env = os.environ.get("CHAT_IMPORT_JSON")
if _legacy_env is None:
    LEGACY_JSON = BASE_DIR / "data.json"
elif _legacy_env.strip() == "":
    LEGACY_JSON = None
else:
    LEGACY_JSON = Path(_legacy_env)

SCHEMA_VERSION = 2
DEFAULT_ROOM_ID = 1
DEFAULT_ROOM_NAME = "大厅"

ALLOWED_IMAGE_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

# ---------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------
lock = threading.RLock()
db = None

state = {"next_id": 1, "next_rev": 1}

# 在线状态（不持久化）：
#   client_id  = 一个窗口/连接（sessionStorage，负责心跳）
#   device_id  = 一个人/浏览器（localStorage，同一浏览器多窗口共享）
#   room_id    = 这个窗口当前在哪个房间
users = {}      # client_id -> {"device_id", "name", "room_id", "last_seen", "joined_at"}
rate_log = {}   # device_id -> [发送时间戳]

_last_prune = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    room_id    INTEGER NOT NULL DEFAULT 1,
    rev        INTEGER NOT NULL,
    uuid       TEXT,
    client_id  TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'text',   -- text | image
    text       TEXT NOT NULL,
    ts         REAL NOT NULL,
    time       TEXT NOT NULL,
    system     INTEGER NOT NULL DEFAULT 0,
    revoked    INTEGER NOT NULL DEFAULT 0,
    revoked_at REAL,
    edited_at  REAL,
    edit_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS message_audience (
    message_id INTEGER NOT NULL,
    device_id  TEXT NOT NULL,
    PRIMARY KEY (message_id, device_id)
);

CREATE TABLE IF NOT EXISTS read_positions (
    device_id TEXT NOT NULL,
    room_id   INTEGER NOT NULL DEFAULT 1,
    name      TEXT NOT NULL,
    read_id   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, room_id)
);

CREATE TABLE IF NOT EXISTS avatars (
    device_id  TEXT PRIMARY KEY,
    mime       TEXT NOT NULL,
    data       BLOB NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    message_id INTEGER PRIMARY KEY,
    mime       TEXT NOT NULL,
    data       BLOB NOT NULL,
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_rev ON messages(rev);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_uuid ON messages(client_id, uuid);
-- 注意：room_id 上的索引放在 migrate_schema() 里建，因为 v1 的 messages 还没有这一列
"""


def get_meta(key, default):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row[0]) if row else default


def set_meta(key, value):
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _columns(table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def ensure_default_room():
    if db.execute("SELECT 1 FROM rooms WHERE id = ?", (DEFAULT_ROOM_ID,)).fetchone() is None:
        db.execute(
            "INSERT INTO rooms(id, name, created_at, created_by) VALUES(?,?,?,?)",
            (DEFAULT_ROOM_ID, DEFAULT_ROOM_NAME, time.time(), ""),
        )


def migrate_schema():
    """v1（单房间）→ v2（多房间 + 图片 + 头像），旧数据全部进「大厅」"""
    messages_cols = _columns("messages")
    need_messages = "room_id" not in messages_cols or "kind" not in messages_cols
    rp_cols = _columns("read_positions")
    need_rp = "room_id" not in rp_cols

    if need_messages:
        with db:
            if "room_id" not in messages_cols:
                db.execute("ALTER TABLE messages ADD COLUMN room_id INTEGER NOT NULL DEFAULT 1")
            if "kind" not in messages_cols:
                db.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
            ensure_default_room()
        print("数据库升级：messages 已加上 room_id / kind（旧消息归入大厅）")

    if need_rp:
        with db:
            db.execute("ALTER TABLE read_positions RENAME TO read_positions_v1")
            db.execute("""
                CREATE TABLE read_positions (
                    device_id TEXT NOT NULL,
                    room_id   INTEGER NOT NULL DEFAULT 1,
                    name      TEXT NOT NULL,
                    read_id   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (device_id, room_id)
                )""")
            db.execute("""
                INSERT INTO read_positions(device_id, room_id, name, read_id)
                SELECT device_id, 1, name, read_id FROM read_positions_v1""")
            db.execute("DROP TABLE read_positions_v1")
            ensure_default_room()
        print("数据库升级：已读水位改为按房间（旧数据归入大厅）")

    with db:
        ensure_default_room()
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_room_rev ON messages(room_id, rev)")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def migrate_legacy_json():
    """把更早的 data.json 一次性导入（保留老聊天记录）"""
    if LEGACY_JSON is None or not LEGACY_JSON.exists():
        return
    if db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] > 0:
        return
    try:
        raw = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
        msgs = (raw.get("messages") or [])[-MAX_KEEP:]
        with db:
            ensure_default_room()
            for m in msgs:
                now = float(m.get("ts") or time.time())
                db.execute(
                    "INSERT OR IGNORE INTO messages "
                    "(id, room_id, rev, uuid, client_id, device_id, name, kind, text, ts, time, "
                    " system, revoked, revoked_at, edited_at, edit_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(m["id"]), DEFAULT_ROOM_ID, int(m.get("rev") or 0), m.get("uuid"),
                        str(m.get("client_id") or ""),
                        str(m.get("device_id") or m.get("client_id") or ""),
                        str(m.get("name") or ""), "text", str(m.get("text") or ""),
                        now, str(m.get("time") or time.strftime("%H:%M:%S", time.localtime(now))),
                        1 if m.get("system") else 0,
                        1 if m.get("revoked") else 0,
                        float(m["revoked_at"]) if m.get("revoked_at") else None,
                        float(m["edited_at"]) if m.get("edited_at") else None,
                        int(m.get("edit_count") or 0),
                    ),
                )
                for dev in (m.get("audience") or []):
                    db.execute(
                        "INSERT OR IGNORE INTO message_audience(message_id, device_id) VALUES(?,?)",
                        (int(m["id"]), str(dev)),
                    )
            for dev, rp in (raw.get("read_positions") or {}).items():
                if isinstance(rp, dict):
                    db.execute(
                        "INSERT OR IGNORE INTO read_positions(device_id, room_id, name, read_id) "
                        "VALUES(?,?,?,?)",
                        (str(dev), DEFAULT_ROOM_ID, str(rp.get("name") or ""),
                         int(rp.get("read_id") or 0)),
                    )
            if raw.get("next_id"):
                set_meta("next_id", int(raw["next_id"]))
            if raw.get("next_rev"):
                set_meta("next_rev", int(raw["next_rev"]))
        LEGACY_JSON.replace(LEGACY_JSON.with_name(LEGACY_JSON.name + ".migrated"))
        print(f"已从 data.json 迁移 {len(msgs)} 条消息到 SQLite")
    except Exception as exc:
        print("迁移 data.json 失败（旧文件保留）：", exc)


def init_db():
    global db
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    db.execute("DROP INDEX IF EXISTS idx_messages_uuid")
    db.commit()

    migrate_legacy_json()
    migrate_schema()

    max_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    max_rev = db.execute("SELECT COALESCE(MAX(rev), 0) FROM messages").fetchone()[0]
    state["next_id"] = max(get_meta("next_id", 1), max_id + 1)
    state["next_rev"] = max(get_meta("next_rev", 1), max_rev + 1)
    count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    rooms = db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    print(f"数据库: {DB_FILE}（{count} 条消息 / {rooms} 个房间，新消息 id 从 {state['next_id']}）")


# ---------------------------------------------------------------------
# 查询 / 序列化
# ---------------------------------------------------------------------
def row_to_msg(row, audience):
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "rev": row["rev"],
        "uuid": row["uuid"],
        "client_id": row["client_id"],
        "device_id": row["device_id"],
        "name": row["name"],
        "kind": row["kind"],
        "text": row["text"],
        "ts": row["ts"],
        "time": row["time"],
        "system": bool(row["system"]),
        "revoked": bool(row["revoked"]),
        "revoked_at": row["revoked_at"],
        "edited_at": row["edited_at"],
        "edit_count": row["edit_count"],
        "audience": audience,
    }


def audiences_for(ids):
    if not ids:
        return {}
    out = {i: [] for i in ids}
    marks = ",".join("?" * len(ids))
    for row in db.execute(
        f"SELECT message_id, device_id FROM message_audience WHERE message_id IN ({marks})",
        list(ids),
    ):
        out[row[0]].append(row[1])
    return out


def public_msg(m):
    return {
        "id": m["id"],
        "room_id": m.get("room_id", DEFAULT_ROOM_ID),
        "rev": m.get("rev", 0),
        "uuid": m.get("uuid"),
        "client_id": m.get("client_id", ""),
        "device_id": m.get("device_id", m.get("client_id", "")),
        "name": m.get("name", ""),
        "kind": m.get("kind", "text"),
        "text": m.get("text", ""),
        "ts": m.get("ts", 0),
        "time": m.get("time", ""),
        "system": m.get("system", False),
        "revoked": m.get("revoked", False),
        "edited_at": m.get("edited_at"),
        "edit_count": m.get("edit_count", 0),
        "audience": m.get("audience", []),
    }


def rows_to_public(rows):
    aud = audiences_for([r["id"] for r in rows])
    return [public_msg(row_to_msg(r, aud.get(r["id"], []))) for r in rows]


def get_message(msg_id):
    row = db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        return None
    return row_to_msg(row, audiences_for([msg_id]).get(msg_id, []))


def trim_room(room_id):
    """每个房间只留最近 MAX_KEEP 条，连带删掉被裁掉的图片（调用前需在事务里）"""
    cutoff = db.execute(
        "SELECT id FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (room_id, MAX_KEEP),
    ).fetchone()
    if cutoff:
        db.execute(
            "DELETE FROM images WHERE message_id IN "
            "(SELECT id FROM messages WHERE room_id = ? AND id < ?)",
            (room_id, cutoff[0]),
        )
        db.execute("DELETE FROM message_audience WHERE message_id < ?", (cutoff[0],))
        db.execute("DELETE FROM messages WHERE room_id = ? AND id < ?", (room_id, cutoff[0]))


def avatars_map():
    return {r["device_id"]: r["version"] for r in db.execute("SELECT device_id, version FROM avatars")}


def room_exists(room_id):
    return db.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone() is not None


def rooms_payload(now, device):
    """会话列表：每个房间的最后一条、未读数、在线人数"""
    rows = db.execute("SELECT * FROM rooms").fetchall()
    out = []
    for r in rows:
        last = db.execute(
            "SELECT id, kind, text, name, time, ts, revoked, system FROM messages "
            "WHERE room_id = ? ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        read_id = 0
        if device:
            rp = db.execute(
                "SELECT read_id FROM read_positions WHERE device_id = ? AND room_id = ?",
                (device, r["id"]),
            ).fetchone()
            read_id = int(rp[0]) if rp else 0
        unread = 0
        if device:
            unread = db.execute(
                "SELECT COUNT(*) FROM messages WHERE room_id = ? AND id > ? "
                "AND system = 0 AND revoked = 0 AND device_id <> ?",
                (r["id"], read_id, device),
            ).fetchone()[0]
        online = len({
            u["device_id"] for u in users.values()
            if u["room_id"] == r["id"] and now - u["last_seen"] <= ONLINE_TIMEOUT
        })

        if last is None:
            preview, last_ts, last_time = "", 0, ""
        else:
            last_ts, last_time = last["ts"], last["time"]
            if last["system"]:
                preview = last["text"]
            elif last["revoked"]:
                preview = "消息已撤回"
            elif last["kind"] == "image":
                preview = "[图片]"
            else:
                preview = last["text"][:40]

        out.append({
            "id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "last_text": preview,
            "last_ts": last_ts,
            "last_time": last_time,
            "unread": unread,
            "online": online,
        })

    # 大厅永远排第一，其余按最近活跃排序
    out.sort(key=lambda x: (0 if x["id"] == DEFAULT_ROOM_ID else 1,
                            -(x["last_ts"] or x["created_at"])))
    return out


# ---------------------------------------------------------------------
# 在线状态
# ---------------------------------------------------------------------
def prune_ephemeral(now):
    global _last_prune
    if now - _last_prune < 60:
        return
    _last_prune = now
    keep = max(600, ONLINE_TIMEOUT * 10)
    for cid in [c for c, u in users.items() if now - u["last_seen"] > keep]:
        users.pop(cid, None)
    for dev in [d for d, log in rate_log.items() if not log or now - log[-1] > keep]:
        rate_log.pop(dev, None)


def online_members(room_id, now=None):
    """某个房间里的在线的人（按设备去重）。调用前必须持有 lock。"""
    now = now or time.time()
    people = {}
    for u in users.values():
        if u["room_id"] != room_id:
            continue
        if now - u["last_seen"] > ONLINE_TIMEOUT:
            continue
        p = people.setdefault(u["device_id"], {
            "device_id": u["device_id"], "name": u["name"], "count": 0,
            "joined_at": u["joined_at"], "last_seen": 0,
        })
        p["count"] += 1
        p["joined_at"] = min(p["joined_at"], u["joined_at"])
        if u["last_seen"] > p["last_seen"]:
            p["last_seen"] = u["last_seen"]
            p["name"] = u["name"]
    return [
        {"device_id": p["device_id"], "name": p["name"], "count": p["count"],
         "joined_at": p["joined_at"]}
        for p in sorted(people.values(), key=lambda x: x["joined_at"])
    ]


# ---------------------------------------------------------------------
# 发消息
# ---------------------------------------------------------------------
def new_message(client_id, device_id, name, text, room_id,
                kind="text", msg_uuid=None, system=False):
    now = time.time()
    msg_id = state["next_id"]
    state["next_id"] += 1
    rev = state["next_rev"]
    state["next_rev"] += 1

    # 已读分母：同一房间、发送那一刻在线的其他“人”（按设备去重）
    audience = []
    if not system:
        for u in users.values():
            if u["room_id"] != room_id:
                continue
            if u["device_id"] == device_id:
                continue
            if now - u["last_seen"] > ONLINE_TIMEOUT:
                continue
            if u["device_id"] not in audience:
                audience.append(u["device_id"])

    with db:
        db.execute(
            "INSERT INTO messages "
            "(id, room_id, rev, uuid, client_id, device_id, name, kind, text, ts, time, "
            " system, revoked, revoked_at, edited_at, edit_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,0)",
            (msg_id, room_id, rev, msg_uuid, client_id, device_id, name, kind, text,
             now, time.strftime("%H:%M:%S", time.localtime(now)), 1 if system else 0),
        )
        for dev in audience:
            db.execute(
                "INSERT OR IGNORE INTO message_audience(message_id, device_id) VALUES(?,?)",
                (msg_id, dev),
            )
        set_meta("next_id", state["next_id"])
        set_meta("next_rev", state["next_rev"])
        trim_room(room_id)

    return {
        "id": msg_id, "room_id": room_id, "rev": rev, "uuid": msg_uuid,
        "client_id": client_id, "device_id": device_id, "name": name, "kind": kind,
        "text": text, "ts": now, "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "system": system, "revoked": False, "edited_at": None, "edit_count": 0,
        "audience": audience,
    }


# ---------------------------------------------------------------------
# 各种校验
# ---------------------------------------------------------------------
def fmt_window(seconds):
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    if seconds >= 60:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds} 秒"


def _has_surrogate(text):
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _clean_cid(raw):
    cid = str(raw or "").strip()
    if not cid or len(cid) > 64 or _has_surrogate(cid):
        return None
    return cid


def _clean_device(raw):
    dev = str(raw or "").strip()
    if not dev or len(dev) > 64 or _has_surrogate(dev):
        return None
    return dev


def _clean_name(raw, what="昵称"):
    name = str(raw or "").strip()
    if not name:
        return None, f"{what}不能为空"
    if len(name) > NAME_MAX_LEN:
        return None, f"{what}过长：最多 {NAME_MAX_LEN} 字，当前 {len(name)} 字"
    if _has_surrogate(name):
        return None, f"{what}包含无法识别的字符"
    return name, None


def _clean_text(raw):
    if not isinstance(raw, str):
        return None, "消息内容格式不正确"
    text = raw.strip()
    if not text:
        return None, "消息不能为空"
    if len(text) > MSG_MAX_LEN:
        return None, f"消息过长：最多 {MSG_MAX_LEN} 字，当前 {len(text)} 字"
    if _has_surrogate(text):
        return None, "消息包含无法识别的字符"
    return text, None


def _parse_room_id(raw, default=DEFAULT_ROOM_ID):
    try:
        rid = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return rid if rid > 0 else default


def _sniff_image(data):
    """按魔术字节判断真实类型，防止把任意文件当图片存进来"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def parse_data_url(raw, max_bytes):
    """解析 data:image/xxx;base64,....，校验类型和大小"""
    if not isinstance(raw, str) or not raw.startswith("data:image/"):
        return None, "图片格式不正确"
    head, _, payload = raw.partition(",")
    if not payload:
        return None, "图片数据为空"
    mime = head[5:].split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in ALLOWED_IMAGE_MIMES:
        return None, "只支持 JPEG / PNG / WebP 图片"
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return None, "图片数据不合法"
    if len(data) > max_bytes:
        return None, f"图片太大：最多 {max_bytes // 1024} KB，当前 {len(data) // 1024} KB"
    real = _sniff_image(data)
    if real is None or real != mime:
        return None, "图片内容与格式不符"
    return (mime, data), None


def _err(status, message, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return status, payload


def _recent_join_notice(room_id, name, now):
    text = f"{name} 加入了聊天室"
    row = db.execute(
        "SELECT 1 FROM messages WHERE room_id = ? AND system = 1 AND text = ? AND ts > ? LIMIT 1",
        (room_id, text, now - 10),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------
# 接口：加入 / 状态 / 房间
# ---------------------------------------------------------------------
def handle_join(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    if data.get("device_id") is not None:
        device = _clean_device(data.get("device_id"))
        if device is None:
            return _err(400, "device_id 无效")
    else:
        device = "dev-" + cid
    name, err = _clean_name(data.get("name"))
    if err:
        return _err(400, err)
    room_id = _parse_room_id(data.get("room_id"))
    if not room_exists(room_id):
        return _err(404, "房间不存在")
    silent = bool(data.get("silent"))

    with lock:
        now = time.time()
        known = cid in users
        person_alive = any(
            other != cid and u["device_id"] == device
            and u["room_id"] == room_id and now - u["last_seen"] <= ONLINE_TIMEOUT
            for other, u in users.items()
        )
        users[cid] = {"device_id": device, "name": name, "room_id": room_id,
                      "last_seen": now, "joined_at": now}

        if not known and not person_alive and not silent and not _recent_join_notice(room_id, name, now):
            new_message(cid, device, name, f"{name} 加入了聊天室", room_id, system=True)

        latest = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        rp = db.execute(
            "SELECT read_id FROM read_positions WHERE device_id = ? AND room_id = ?",
            (device, room_id),
        ).fetchone()
        with db:
            if rp is None:
                read_id = latest
                db.execute(
                    "INSERT INTO read_positions(device_id, room_id, name, read_id) VALUES(?,?,?,?)",
                    (device, room_id, name, read_id),
                )
            else:
                read_id = max(0, min(int(rp[0]), latest))
                db.execute(
                    "UPDATE read_positions SET name = ?, read_id = ? "
                    "WHERE device_id = ? AND room_id = ?",
                    (name, read_id, device, room_id),
                )

        payload = {
            "ok": True, "client_id": cid, "device_id": device, "room_id": room_id,
            "read_id": read_id, "latest_id": latest,
            "members": online_members(room_id, now),
            "avatars": avatars_map(),
            "rooms": rooms_payload(now, device),
            "limits": limits_dict(),
        }
    return 200, payload


def limits_dict():
    return {
        "msg_max": MSG_MAX_LEN, "name_max": NAME_MAX_LEN,
        "recall": RECALL_WINDOW, "edit": EDIT_WINDOW,
        "max_keep": MAX_KEEP, "history_page": HISTORY_PAGE,
        "online_timeout": ONLINE_TIMEOUT,
        "rate_count": RATE_LIMIT_COUNT, "rate_window": RATE_LIMIT_WINDOW,
        "image_max": IMAGE_MAX_BYTES, "avatar_max": AVATAR_MAX_BYTES,
        "room_name_max": ROOM_NAME_MAX,
    }


def handle_state(qs):
    cid = str(qs.get("client_id", [""])[0]).strip()
    try:
        rev = int(qs.get("rev", ["0"])[0] or 0)
    except (TypeError, ValueError):
        rev = 0
    room_id = _parse_room_id(qs.get("room_id", [str(DEFAULT_ROOM_ID)])[0])

    with lock:
        if not room_exists(room_id):
            return _err(404, "房间不存在")
        now = time.time()
        prune_ephemeral(now)
        known = bool(cid and cid in users)
        if known:
            users[cid]["last_seen"] = now
            users[cid]["room_id"] = room_id
        device = users.get(cid, {}).get("device_id") if cid else None

        rows = db.execute(
            "SELECT * FROM messages WHERE room_id = ? AND rev > ? ORDER BY id ASC",
            (room_id, rev),
        ).fetchall()
        latest = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        oldest = db.execute(
            "SELECT COALESCE(MIN(id), 0) FROM messages WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        read_positions = {
            r["device_id"]: {"name": r["name"], "read_id": r["read_id"]}
            for r in db.execute("SELECT * FROM read_positions WHERE room_id = ?", (room_id,))
        }
        my_read = read_positions.get(device, {}).get("read_id", 0) if device else 0

        payload = {
            "ok": True,
            "known": known,
            "reset": bool(rev > 0 and rev >= state["next_rev"]),
            "server_time": now,
            "room_id": room_id,
            "messages": rows_to_public(rows),
            "members": online_members(room_id, now),
            "read_positions": read_positions,
            "my_read_id": my_read,
            "oldest_id": oldest,
            "latest_id": latest,
            "rooms": rooms_payload(now, device),
            "avatars": avatars_map(),
            "limits": limits_dict(),
        }
    return 200, payload


def handle_rooms_get(qs):
    cid = str(qs.get("client_id", [""])[0]).strip()
    with lock:
        now = time.time()
        device = users.get(cid, {}).get("device_id") if cid else None
        payload = {"ok": True, "rooms": rooms_payload(now, device)}
    return 200, payload


def handle_rooms_create(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    name, err = _clean_name(data.get("name"), "房间名")
    if err:
        return _err(400, err)
    if len(name) > ROOM_NAME_MAX:
        return _err(400, f"房间名过长：最多 {ROOM_NAME_MAX} 字")

    with lock:
        row = db.execute("SELECT id FROM rooms WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return _err(409, "已经有同名房间了，换一个名字吧")
        with db:
            cur = db.execute(
                "INSERT INTO rooms(name, created_at, created_by) VALUES(?,?,?)",
                (name, time.time(), users.get(cid, {}).get("device_id", "")),
            )
            room_id = cur.lastrowid
        now = time.time()
        payload = {
            "ok": True,
            "room": {
                "id": room_id, "name": name, "created_at": time.time(),
                "last_text": "", "last_ts": 0, "last_time": "", "unread": 0, "online": 0,
            },
            "rooms": rooms_payload(now, users.get(cid, {}).get("device_id")),
        }
    print(f"[{time.strftime('%H:%M:%S')}] 新建房间：{name} (id={room_id})")
    return 200, payload


# ---------------------------------------------------------------------
# 接口：发文字 / 发图片
# ---------------------------------------------------------------------
def _ensure_user(cid, device, name, room_id, now):
    """确保这个窗口在 users 里，且房间/昵称是最新的"""
    if cid in users:
        users[cid].setdefault("device_id", "dev-" + cid)
    else:
        users[cid] = {"device_id": device or ("dev-" + cid), "name": name,
                      "room_id": room_id, "last_seen": now, "joined_at": now}
    users[cid]["name"] = name
    users[cid]["room_id"] = room_id
    users[cid]["last_seen"] = now
    return users[cid]["device_id"]


def _rate_limited(device, now):
    log = rate_log.setdefault(device, [])
    log[:] = [t for t in log if now - t < RATE_LIMIT_WINDOW]
    if len(log) >= RATE_LIMIT_COUNT:
        wait = max(1, int(RATE_LIMIT_WINDOW - (now - log[0]) + 0.999))
        return wait
    log.append(now)
    return 0


def handle_send(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    name, err = _clean_name(data.get("name"))
    if err:
        return _err(400, err)
    text, err = _clean_text(data.get("text"))
    if err:
        return _err(400, err)
    msg_uuid = str(data.get("msg_uuid") or "").strip()
    if not msg_uuid or len(msg_uuid) > 64 or _has_surrogate(msg_uuid):
        return _err(400, "msg_uuid 无效")
    if data.get("device_id") is not None:
        device = _clean_device(data.get("device_id"))
        if device is None:
            return _err(400, "device_id 无效")
    else:
        device = None
    room_id = _parse_room_id(data.get("room_id"))

    with lock:
        if not room_exists(room_id):
            return _err(404, "房间不存在")
        row = db.execute(
            "SELECT * FROM messages WHERE uuid = ? AND client_id = ?", (msg_uuid, cid)
        ).fetchone()
        if row is not None:
            aud = audiences_for([row["id"]]).get(row["id"], [])
            return 200, {"ok": True, "duplicate": True,
                         "msg": public_msg(row_to_msg(row, aud))}

        now = time.time()
        device = _ensure_user(cid, device, name, room_id, now)
        wait = _rate_limited(device, now)
        if wait:
            return _err(429, f"发送太快了，请 {wait} 秒后再试", retry_after=wait)

        with db:
            db.execute(
                "INSERT INTO read_positions(device_id, room_id, name, read_id) VALUES(?,?,?,0) "
                "ON CONFLICT(device_id, room_id) DO UPDATE SET name = excluded.name",
                (device, room_id, name),
            )

        msg = new_message(cid, device, name, text, room_id, msg_uuid=msg_uuid)

    print(f"[{msg['time']}] [房间{room_id}] {name}: {text[:200]}")
    return 200, {"ok": True, "duplicate": False, "msg": public_msg(msg)}


def handle_send_image(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    name, err = _clean_name(data.get("name"))
    if err:
        return _err(400, err)
    msg_uuid = str(data.get("msg_uuid") or "").strip()
    if not msg_uuid or len(msg_uuid) > 64 or _has_surrogate(msg_uuid):
        return _err(400, "msg_uuid 无效")
    if data.get("device_id") is not None:
        device = _clean_device(data.get("device_id"))
        if device is None:
            return _err(400, "device_id 无效")
    else:
        device = None
    room_id = _parse_room_id(data.get("room_id"))

    parsed, err = parse_data_url(data.get("data_url"), IMAGE_MAX_BYTES)
    if err:
        return _err(400, err)
    mime, blob = parsed

    try:
        width = max(0, int(data.get("width") or 0))
        height = max(0, int(data.get("height") or 0))
    except (TypeError, ValueError):
        width = height = 0

    with lock:
        if not room_exists(room_id):
            return _err(404, "房间不存在")
        row = db.execute(
            "SELECT * FROM messages WHERE uuid = ? AND client_id = ?", (msg_uuid, cid)
        ).fetchone()
        if row is not None:
            aud = audiences_for([row["id"]]).get(row["id"], [])
            return 200, {"ok": True, "duplicate": True,
                         "msg": public_msg(row_to_msg(row, aud))}

        now = time.time()
        device = _ensure_user(cid, device, name, room_id, now)
        wait = _rate_limited(device, now)
        if wait:
            return _err(429, f"发送太快了，请 {wait} 秒后再试", retry_after=wait)

        with db:
            db.execute(
                "INSERT INTO read_positions(device_id, room_id, name, read_id) VALUES(?,?,?,0) "
                "ON CONFLICT(device_id, room_id) DO UPDATE SET name = excluded.name",
                (device, room_id, name),
            )

        msg = new_message(cid, device, name, "", room_id, kind="image", msg_uuid=msg_uuid)
        with db:
            db.execute(
                "INSERT OR REPLACE INTO images(message_id, mime, data, width, height) "
                "VALUES(?,?,?,?,?)",
                (msg["id"], mime, sqlite3.Binary(blob), width, height),
            )

    print(f"[{msg['time']}] [房间{room_id}] {name} 发了一张图片 ({len(blob) // 1024} KB)")
    return 200, {"ok": True, "duplicate": False, "msg": public_msg(msg)}


# ---------------------------------------------------------------------
# 接口：历史 / 已读 / 撤回 / 编辑 / 离线
# ---------------------------------------------------------------------
def handle_history(qs):
    room_id = _parse_room_id(qs.get("room_id", [str(DEFAULT_ROOM_ID)])[0])
    try:
        before = int(qs.get("before", ["0"])[0] or 0)
    except (TypeError, ValueError):
        before = 0
    try:
        limit = int(qs.get("limit", [str(HISTORY_PAGE)])[0] or HISTORY_PAGE)
    except (TypeError, ValueError):
        limit = HISTORY_PAGE
    limit = max(1, min(limit, 100))

    with lock:
        if not room_exists(room_id):
            return _err(404, "房间不存在")
        if before > 0:
            rows = db.execute(
                "SELECT * FROM messages WHERE room_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (room_id, before, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
        rows = list(reversed(rows))
        has_more = bool(rows and db.execute(
            "SELECT 1 FROM messages WHERE room_id = ? AND id < ? LIMIT 1",
            (room_id, rows[0]["id"]),
        ).fetchone())
        oldest = db.execute(
            "SELECT COALESCE(MIN(id), 0) FROM messages WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        latest = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        payload = {
            "ok": True, "room_id": room_id, "messages": rows_to_public(rows),
            "has_more": has_more, "oldest_id": oldest, "latest_id": latest,
        }
    return 200, payload


def handle_read(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    try:
        read_id = int(data.get("read_id", 0))
    except (TypeError, ValueError):
        return _err(400, "read_id 无效")
    room_id = _parse_room_id(data.get("room_id"))

    with lock:
        if cid not in users:
            return _err(403, "请先加入聊天室")
        if not room_exists(room_id):
            return _err(404, "房间不存在")
        device = users[cid]["device_id"]
        name = users[cid]["name"]
        latest = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        read_id = max(0, min(read_id, latest))
        row = db.execute(
            "SELECT read_id FROM read_positions WHERE device_id = ? AND room_id = ?",
            (device, room_id),
        ).fetchone()
        with db:
            if row is None:
                db.execute(
                    "INSERT INTO read_positions(device_id, room_id, name, read_id) VALUES(?,?,?,?)",
                    (device, room_id, name, read_id),
                )
            elif read_id > int(row[0]):
                db.execute(
                    "UPDATE read_positions SET name = ?, read_id = ? "
                    "WHERE device_id = ? AND room_id = ?",
                    (name, read_id, device, room_id),
                )
            else:
                db.execute(
                    "UPDATE read_positions SET name = ? WHERE device_id = ? AND room_id = ?",
                    (name, device, room_id),
                )
        final = db.execute(
            "SELECT read_id FROM read_positions WHERE device_id = ? AND room_id = ?",
            (device, room_id),
        ).fetchone()[0]
        payload = {"ok": True, "read_id": int(final)}
    return 200, payload


def handle_revoke(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    try:
        msg_id = int(data.get("id", 0))
    except (TypeError, ValueError):
        return _err(400, "消息 id 无效")

    with lock:
        m = get_message(msg_id)
        if m is None:
            return _err(404, "消息不存在或已被清理")
        if m["system"]:
            return _err(400, "系统消息不能撤回")
        requester = users.get(cid)
        if requester is None:
            return _err(403, "请先加入聊天室")
        if m["device_id"] != requester["device_id"]:
            return _err(403, "只能撤回自己的消息")
        if m["revoked"]:
            return _err(400, "该消息已经撤回过了")
        if time.time() - m["ts"] > RECALL_WINDOW:
            return _err(403, f"超过 {fmt_window(RECALL_WINDOW)}，无法撤回")

        new_rev = state["next_rev"]
        state["next_rev"] += 1
        with db:
            db.execute(
                "UPDATE messages SET revoked = 1, text = '', revoked_at = ?, rev = ? WHERE id = ?",
                (time.time(), new_rev, msg_id),
            )
            if m["kind"] == "image":
                db.execute("DELETE FROM images WHERE message_id = ?", (msg_id,))
            set_meta("next_rev", state["next_rev"])
        payload = {"ok": True, "msg": public_msg(get_message(msg_id))}
    print(f"[{time.strftime('%H:%M:%S')}] {m['name']} 撤回了一条消息 (id={msg_id})")
    return 200, payload


def handle_edit(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    try:
        msg_id = int(data.get("id", 0))
    except (TypeError, ValueError):
        return _err(400, "消息 id 无效")
    text, err = _clean_text(data.get("text"))
    if err:
        return _err(400, err)

    with lock:
        m = get_message(msg_id)
        if m is None:
            return _err(404, "消息不存在或已被清理")
        if m["system"]:
            return _err(400, "系统消息不能编辑")
        if m["kind"] != "text":
            return _err(400, "图片消息不能编辑")
        requester = users.get(cid)
        if requester is None:
            return _err(403, "请先加入聊天室")
        if m["device_id"] != requester["device_id"]:
            return _err(403, "只能编辑自己的消息")
        if m["revoked"]:
            return _err(400, "消息已撤回，不能编辑")
        if time.time() - m["ts"] > EDIT_WINDOW:
            return _err(403, f"超过 {fmt_window(EDIT_WINDOW)}，无法编辑")

        new_rev = state["next_rev"]
        state["next_rev"] += 1
        with db:
            db.execute(
                "UPDATE messages SET text = ?, edited_at = ?, edit_count = edit_count + 1, "
                "rev = ? WHERE id = ?",
                (text, time.time(), new_rev, msg_id),
            )
            set_meta("next_rev", state["next_rev"])
        payload = {"ok": True, "msg": public_msg(get_message(msg_id))}
    print(f"[{time.strftime('%H:%M:%S')}] {m['name']} 编辑了消息 (id={msg_id})")
    return 200, payload


def handle_leave(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    with lock:
        users.pop(cid, None)
    return 200, {"ok": True}


# ---------------------------------------------------------------------
# 接口：头像 / 图片字节
# ---------------------------------------------------------------------
def handle_avatar_upload(data):
    cid = _clean_cid(data.get("client_id"))
    if cid is None:
        return _err(400, "client_id 无效")
    if cid not in users:
        return _err(403, "请先加入聊天室")
    parsed, err = parse_data_url(data.get("data_url"), AVATAR_MAX_BYTES)
    if err:
        return _err(400, err)
    mime, blob = parsed
    device = users[cid]["device_id"]

    with lock:
        row = db.execute(
            "SELECT version FROM avatars WHERE device_id = ?", (device,)
        ).fetchone()
        version = (int(row[0]) + 1) if row else 1
        with db:
            db.execute(
                "INSERT INTO avatars(device_id, mime, data, version, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET mime = excluded.mime, data = excluded.data, "
                "version = excluded.version, updated_at = excluded.updated_at",
                (device, mime, sqlite3.Binary(blob), version, time.time()),
            )
        payload = {"ok": True, "device_id": device, "version": version,
                   "avatars": avatars_map()}
    print(f"[{time.strftime('%H:%M:%S')}] {users[cid]['name']} 更换了头像")
    return 200, payload


def handle_avatar_get(qs):
    device = str(qs.get("device_id", [""])[0]).strip()
    if not device:
        return None, None, None
    with lock:
        row = db.execute(
            "SELECT mime, data, version FROM avatars WHERE device_id = ?", (device,)
        ).fetchone()
    if row is None:
        return None, None, None
    return row["data"], row["mime"], row["version"]


def handle_image_get(qs):
    try:
        msg_id = int(qs.get("message_id", ["0"])[0] or 0)
    except (TypeError, ValueError):
        return None, None
    with lock:
        row = db.execute(
            "SELECT i.mime, i.data, m.revoked, m.kind FROM images i "
            "JOIN messages m ON m.id = i.message_id WHERE i.message_id = ?",
            (msg_id,),
        ).fetchone()
    if row is None or row["revoked"] or row["kind"] != "image":
        return None, None
    return row["data"], row["mime"]


# ---------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------
class ChatHandler(BaseHTTPRequestHandler):
    server_version = "ChatRoom/4.0"
    protocol_version = "HTTP/1.1"

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, file_name, content_type):
        path = STATIC_DIR / file_name
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_binary(self, body, content_type, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # URL 里带版本号，所以可以放心长缓存
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, max_bytes=100_000):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法的 JSON")

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                self.send_file("index.html", "text/html; charset=utf-8")
            elif path == "/style.css":
                self.send_file("style.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self.send_file("app.js", "application/javascript; charset=utf-8")
            elif path == "/state":
                status, payload = handle_state(qs)
                self.send_json(payload, status)
            elif path == "/history":
                status, payload = handle_history(qs)
                self.send_json(payload, status)
            elif path == "/rooms":
                status, payload = handle_rooms_get(qs)
                self.send_json(payload, status)
            elif path == "/avatar":
                data, mime, _v = handle_avatar_get(qs)
                if data is None:
                    self.send_error(404, "Not Found")
                else:
                    self.send_binary(data, mime, cache=True)
            elif path == "/image":
                data, mime = handle_image_get(qs)
                if data is None:
                    self.send_error(404, "Not Found")
                else:
                    self.send_binary(data, mime, cache=True)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"ok": False, "error": "服务器内部错误"}, 500)
            except Exception:
                pass

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        routes = {
            "/join": (handle_join, 200_000),
            "/send": (handle_send, 200_000),
            "/send_image": (handle_send_image, IMAGE_MAX_BYTES * 2 + 200_000),
            "/read": (handle_read, 200_000),
            "/revoke": (handle_revoke, 200_000),
            "/edit": (handle_edit, 200_000),
            "/leave": (handle_leave, 200_000),
            "/rooms": (handle_rooms_create, 200_000),
            "/avatar": (handle_avatar_upload, AVATAR_MAX_BYTES * 2 + 100_000),
        }
        entry = routes.get(path)
        try:
            if entry is None:
                self.send_error(404, "Not Found")
                return
            handler, max_bytes = entry
            data = self.read_json(max_bytes)
            status, payload = handler(data)
            self.send_json(payload, status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"ok": False, "error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def log_message(self, fmt, *args):
        path = getattr(self, "path", "")
        if "/state" in path or "/read" in path:
            return
        print(time.strftime("[%H:%M:%S]"), self.address_string(), fmt % args)


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 允许“多个进程绑同一端口”，会导致新请求被旧进程接走
    # （调试时踩过：三个旧服务器同时监听 9999）。所以 Windows 下关掉；Unix 下保留。
    allow_reuse_address = (os.name != "nt")

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


DISCOVERY_MAGIC = b"CHATROOM_DISCOVER_V1"


def start_discovery(port):
    """UDP 广播应答：App 不用知道 IP 也能找到服务器"""

    def loop():
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name != "nt":   # Windows 下 SO_REUSEADDR 会允许多个进程绑同一端口
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            udp.bind(("0.0.0.0", port))
        except OSError as exc:
            print(f"UDP 自动发现启动失败（不影响手动填地址）：{exc}")
            return
        print(f"  自动发现: 已开启（App 里点「自动查找」即可，UDP {port}）")
        while True:
            try:
                data, addr = udp.recvfrom(1024)
            except OSError:
                break
            if data.strip() == DISCOVERY_MAGIC:
                reply = json.dumps({"service": "chatroom", "port": port}).encode("utf-8")
                try:
                    udp.sendto(reply, addr)
                except OSError:
                    pass

    thread = threading.Thread(target=loop, daemon=True, name="chatroom-discovery")
    thread.start()
    return thread


def main():
    init_db()
    try:
        server = ChatServer((HOST, PORT), ChatHandler)
    except OSError as exc:
        print(f"启动失败：端口 {PORT} 已被占用（多半是聊天室已经开着了）。")
        print("  解决办法：先关掉已经打开的那个聊天室窗口；或者改本文件顶部的 PORT。")
        print(f"  系统错误：{exc}")
        return
    ip = local_ip()
    print("=" * 62)
    print("  局域网聊天室已启动（v3：多房间 / 图片 / 头像，SQLite 存储）")
    print(f"  本机访问:   http://127.0.0.1:{PORT}")
    print(f"  局域网访问: http://{ip}:{PORT}   （把这个地址发给同一个 WiFi 下的人）")
    print(f"  数据库:     {DB_FILE}")
    print(f"  规则:       每房保留 {MAX_KEEP} 条 | 撤回 {fmt_window(RECALL_WINDOW)} | "
          f"编辑 {fmt_window(EDIT_WINDOW)} | 限流 {RATE_LIMIT_COUNT} 条/{RATE_LIMIT_WINDOW} 秒")
    print(f"  图片:       单张 ≤ {IMAGE_MAX_BYTES // 1024} KB，头像 ≤ {AVATAR_MAX_BYTES // 1024} KB")
    print("  人数口径:   按浏览器（设备）去重，分房间统计")
    print("  停止服务:   Ctrl + C")
    print("=" * 62)
    start_discovery(PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务器…")
    finally:
        server.server_close()
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
