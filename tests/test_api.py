#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聊天室后端验收脚本（纯标准库，不依赖 pytest）

运行：
    .venv\\Scripts\\python.exe tests\\test_api.py -v

用独立端口（9992-9998）启动 server.py 的临时实例，用临时数据库，
把 10 项基础能力 + v3 的多房间 / 图片 / 头像逐条断言一遍。
不影响你正在用的 9999 端口和 chat.db。
"""

import base64
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# 1x1 的合法 PNG，用来测图片上传
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_1PX_URL = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()


class ServerProcess:
    def __init__(self, port, db_file, **env_overrides):
        self.port = port
        self.db_file = Path(db_file)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="chat-test-"))
        self.log_path = self.tmp_dir / "server.log"
        self.log = None
        self.proc = None
        self.env = {
            "CHAT_PORT": str(port),
            "CHAT_DB": str(self.db_file),
            # 测试默认不做旧 data.json 迁移，避免把项目里的真实数据带进测试库
            "CHAT_IMPORT_JSON": "",
            "CHAT_ONLINE_TIMEOUT": "3",
            "CHAT_RECALL_WINDOW": "2",
            "CHAT_EDIT_WINDOW": "2",
            "CHAT_RATE_LIMIT": "200",
            "CHAT_RATE_WINDOW": "5",
            "CHAT_MAX_KEEP": "50",
            "PYTHONIOENCODING": "utf-8",
        }
        self.env.update({k: str(v) for k, v in env_overrides.items()})

    def start(self):
        env = os.environ.copy()
        env.update(self.env)
        self.log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [PY, str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        self._wait_ready()

    def _wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("服务器提前退出：\n" + self.read_log())
            try:
                request(self.port, "GET", "/state")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("服务器没有按时启动：\n" + self.read_log())

    def read_log(self):
        try:
            self.log.flush()
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<无法读取日志>"

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if self.log:
            self.log.close()
            self.log = None


def request(port, method, path, body=None):
    """返回 (HTTP 状态码, 解析后的 JSON 字典)"""
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": raw}


def fetch_bytes(port, path):
    """返回 (状态码, Content-Type, 字节)；404 不抛异常"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type"), exc.read()


def uid(prefix):
    return f"{prefix}-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"


def join(port, cid, name, device=None, room_id=1):
    status, data = request(port, "POST", "/join", {
        "client_id": cid, "device_id": device or ("dev-" + cid),
        "name": name, "room_id": room_id,
    })
    assert status == 200, data
    return data


def send(port, cid, name, text, msg_uuid=None, room_id=1):
    msg_uuid = msg_uuid or uid("uuid")
    return request(port, "POST", "/send", {
        "client_id": cid, "device_id": "dev-" + cid, "name": name,
        "text": text, "msg_uuid": msg_uuid, "room_id": room_id,
    })


def send_image(port, cid, name, data_url=PNG_1PX_URL, msg_uuid=None, room_id=1):
    msg_uuid = msg_uuid or uid("img")
    return request(port, "POST", "/send_image", {
        "client_id": cid, "device_id": "dev-" + cid, "name": name,
        "data_url": data_url, "msg_uuid": msg_uuid, "room_id": room_id,
        "width": 1, "height": 1,
    })


def state(port, cid="", rev=0, room_id=1):
    status, data = request(
        port, "GET", f"/state?client_id={cid}&rev={rev}&room_id={room_id}")
    assert status == 200, data
    return data


def rooms(port, cid=""):
    status, data = request(port, "GET", f"/rooms?client_id={cid}")
    assert status == 200, data
    return data


# ---------------------------------------------------------------------
# 基础能力（1~9）
# ---------------------------------------------------------------------
class TestCore(unittest.TestCase):
    PORT = 9998

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="chat-core-", ignore_cleanup_errors=True)
        cls.server = ServerProcess(cls.PORT, Path(cls.tmp.name) / "chat.db")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_01_static_page(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.PORT}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
            self.assertIn("聊天室", html)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))

    def test_02_join_send_sync(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "小明")
        join(self.PORT, b, "小红")

        status, data = send(self.PORT, a, "小明", "你好，这是第一条消息")
        self.assertEqual(status, 200)
        msg = data["msg"]
        self.assertEqual(msg["text"], "你好，这是第一条消息")
        self.assertEqual(msg["kind"], "text")
        self.assertEqual(msg["room_id"], 1)

        st = state(self.PORT, b)
        remote = [m for m in st["messages"] if m["id"] == msg["id"]]
        self.assertEqual(len(remote), 1)
        self.assertIn("dev-" + b, remote[0]["audience"])
        self.assertNotIn("dev-" + a, remote[0]["audience"], "自己不算在已读分母里")
        self.assertFalse(st.get("reset"))
        self.assertTrue(state(self.PORT, b, rev=999999).get("reset"))

    def test_03_validation(self):
        a = uid("a")
        join(self.PORT, a, "校验测试")

        status, data = send(self.PORT, a, "校验测试", "   ")
        self.assertEqual(status, 400)
        self.assertIn("不能为空", data["error"])

        status, data = send(self.PORT, a, "校验测试", "长" * 2001)
        self.assertEqual(status, 400)
        self.assertIn("消息过长", data["error"])

        status, data = request(self.PORT, "POST", "/send", {
            "client_id": a, "name": "名" * 21, "text": "hi", "msg_uuid": uid("u"),
        })
        self.assertEqual(status, 400)
        self.assertIn("昵称过长", data["error"])

        status, data = request(self.PORT, "POST", "/send", {
            "client_id": a, "name": "校验测试", "text": "hi",
        })
        self.assertEqual(status, 400)
        self.assertIn("msg_uuid", data["error"])

        status, data = send(self.PORT, a, "校验测试", "\ud800坏字符")
        self.assertEqual(status, 400)
        self.assertIn("无法识别", data["error"])
        self.assertTrue(state(self.PORT, a)["ok"])

    def test_04_dedupe(self):
        a = uid("a")
        join(self.PORT, a, "重试测试")
        msg_uuid = uid("dup")
        text = "只会出现一次的消息" + msg_uuid

        status1, data1 = send(self.PORT, a, "重试测试", text, msg_uuid)
        status2, data2 = send(self.PORT, a, "重试测试", text, msg_uuid)
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)
        self.assertFalse(data1.get("duplicate"))
        self.assertTrue(data2.get("duplicate"))
        self.assertEqual(data1["msg"]["id"], data2["msg"]["id"])

        st = state(self.PORT, a)
        self.assertEqual(len([m for m in st["messages"] if m["text"] == text]), 1)

    def test_05_recall(self):
        a, b, c = uid("a"), uid("b"), uid("c")
        join(self.PORT, a, "甲")
        join(self.PORT, b, "乙")

        status, data = send(self.PORT, a, "甲", "这条会被撤回")
        msg_id = data["msg"]["id"]

        status, data = request(self.PORT, "POST", "/revoke", {"client_id": b, "id": msg_id})
        self.assertEqual(status, 403)
        self.assertIn("只能撤回自己的消息", data["error"])

        status, data = request(self.PORT, "POST", "/revoke", {"client_id": a, "id": msg_id})
        self.assertEqual(status, 200)
        self.assertTrue(data["msg"]["revoked"])
        self.assertEqual(data["msg"]["text"], "")

        status, data = request(self.PORT, "POST", "/revoke", {"client_id": a, "id": msg_id})
        self.assertEqual(status, 400)
        self.assertIn("已经撤回", data["error"])

        join(self.PORT, c, "丙")
        st = state(self.PORT, c, rev=0)
        seen = [m for m in st["messages"] if m["id"] == msg_id][0]
        self.assertTrue(seen["revoked"])
        self.assertEqual(seen["text"], "")

        status, data = send(self.PORT, a, "甲", "这条超过时限")
        old_id = data["msg"]["id"]
        time.sleep(2.3)
        status, data = request(self.PORT, "POST", "/revoke", {"client_id": a, "id": old_id})
        self.assertEqual(status, 403)
        self.assertIn("无法撤回", data["error"])

    def test_06_edit(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "编辑甲")
        join(self.PORT, b, "编辑乙")

        status, data = send(self.PORT, a, "编辑甲", "原始内容")
        msg = data["msg"]
        self.assertIsNone(msg["edited_at"])

        status, data = request(self.PORT, "POST", "/edit", {
            "client_id": b, "id": msg["id"], "text": "偷改",
        })
        self.assertEqual(status, 403)

        a2 = uid("a2")
        join(self.PORT, a2, "编辑甲", device="dev-" + a)
        status, data = request(self.PORT, "POST", "/edit", {
            "client_id": a2, "id": msg["id"], "text": "修改后的内容",
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["msg"]["text"], "修改后的内容")
        self.assertEqual(data["msg"]["edit_count"], 1)
        self.assertGreater(data["msg"]["rev"], msg["rev"])

        st = state(self.PORT, b, rev=msg["rev"])
        updated = [m for m in st["messages"] if m["id"] == msg["id"]]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["text"], "修改后的内容")

        status, data = send(self.PORT, a, "编辑甲", "马上撤回")
        mid = data["msg"]["id"]
        request(self.PORT, "POST", "/revoke", {"client_id": a, "id": mid})
        status, data = request(self.PORT, "POST", "/edit", {
            "client_id": a, "id": mid, "text": "还想改",
        })
        self.assertEqual(status, 400)
        self.assertIn("已撤回", data["error"])

        status, data = send(self.PORT, a, "编辑甲", "这条超过编辑时限")
        old_id = data["msg"]["id"]
        time.sleep(2.3)
        status, data = request(self.PORT, "POST", "/edit", {
            "client_id": a, "id": old_id, "text": "改不动了",
        })
        self.assertEqual(status, 403)
        self.assertIn("无法编辑", data["error"])

    def test_07_history_paging(self):
        a = uid("a")
        join(self.PORT, a, "历史测试")
        marker = uid("hist")
        sent_ids = []
        for i in range(5):
            status, data = send(self.PORT, a, "历史测试", f"{marker}-第{i}条")
            self.assertEqual(status, 200)
            sent_ids.append(data["msg"]["id"])

        status, page1 = request(self.PORT, "GET", "/history?room_id=1&limit=3")
        self.assertEqual(status, 200)
        ids1 = [m["id"] for m in page1["messages"]]
        self.assertEqual(ids1, sorted(ids1))
        self.assertEqual(ids1[-1], sent_ids[-1])
        self.assertTrue(page1["has_more"])

        status, page2 = request(
            self.PORT, "GET", f"/history?room_id=1&before={ids1[0]}&limit=3")
        ids2 = [m["id"] for m in page2["messages"]]
        self.assertEqual(ids2, sorted(ids2))
        self.assertTrue(all(i < ids1[0] for i in ids2))
        self.assertFalse(set(ids1) & set(ids2))

    def test_08_read_receipt(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "已读甲")
        join(self.PORT, b, "已读乙")
        status, data = send(self.PORT, a, "已读甲", "请确认一下已读")
        msg_id = data["msg"]["id"]
        self.assertIn("dev-" + b, data["msg"]["audience"])

        status, resp = request(self.PORT, "POST", "/read", {
            "client_id": b, "read_id": msg_id, "room_id": 1,
        })
        self.assertEqual(status, 200)
        self.assertGreaterEqual(resp["read_id"], msg_id)

        st = state(self.PORT, a)
        self.assertIn("dev-" + b, st["read_positions"])
        self.assertGreaterEqual(st["read_positions"]["dev-" + b]["read_id"], msg_id)
        self.assertEqual(st["read_positions"]["dev-" + b]["name"], "已读乙")

    def test_09_members_leave_timeout(self):
        a, b, d = uid("a"), uid("b"), uid("d")
        join(self.PORT, a, "成员甲")
        join(self.PORT, b, "成员乙")

        def people(st):
            return {m["device_id"]: m for m in st["members"]}

        st = state(self.PORT, a)
        self.assertIn("dev-" + a, people(st))
        self.assertIn("dev-" + b, people(st))

        b2 = uid("b2")
        join(self.PORT, b2, "成员乙", device="dev-" + b)
        st = state(self.PORT, a)
        self.assertEqual(people(st)["dev-" + b]["count"], 2)

        request(self.PORT, "POST", "/leave", {"client_id": b})
        st = state(self.PORT, a)
        self.assertEqual(people(st)["dev-" + b]["count"], 1)

        request(self.PORT, "POST", "/leave", {"client_id": b2})
        st = state(self.PORT, a)
        self.assertNotIn("dev-" + b, people(st))

        join(self.PORT, d, "掉线测试")
        time.sleep(3.6)
        st = state(self.PORT, a)
        self.assertNotIn("dev-" + d, people(st))


# ---------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------
class TestRateLimit(unittest.TestCase):
    PORT = 9997

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="chat-rate-", ignore_cleanup_errors=True)
        cls.server = ServerProcess(
            cls.PORT, Path(cls.tmp.name) / "chat.db",
            CHAT_RATE_LIMIT="3", CHAT_RATE_WINDOW="5",
        )
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_10_rate_limit(self):
        a = uid("a")
        join(self.PORT, a, "手速测试")
        for i in range(3):
            status, data = send(self.PORT, a, "手速测试", f"第{i}条")
            self.assertEqual(status, 200, data)
        status, data = send(self.PORT, a, "手速测试", "第4条")
        self.assertEqual(status, 429)
        self.assertIn("太快", data["error"])
        self.assertGreaterEqual(data.get("retry_after", 0), 1)


# ---------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------
class TestPersistence(unittest.TestCase):
    PORT = 9996

    def test_11_restart_keeps_everything(self):
        tmp = tempfile.TemporaryDirectory(prefix="chat-persist-", ignore_cleanup_errors=True)
        db_file = Path(tmp.name) / "chat.db"
        server = ServerProcess(self.PORT, db_file)
        try:
            server.start()
            a, b = uid("a"), uid("b")
            join(self.PORT, a, "持久甲")
            join(self.PORT, b, "持久乙")
            _, d1 = send(self.PORT, a, "持久甲", "重启前第一条")
            _, d2 = send(self.PORT, a, "持久甲", "重启前第二条")
            m1, m2 = d1["msg"]["id"], d2["msg"]["id"]
            request(self.PORT, "POST", "/read", {"client_id": b, "read_id": m1, "room_id": 1})
            # 新建一个房间，重启后也应该还在
            status, created = request(self.PORT, "POST", "/rooms", {"client_id": a, "name": "重启房间"})
            self.assertEqual(status, 200)

            server.stop()
            server = ServerProcess(self.PORT, db_file)
            server.start()

            status, page = request(self.PORT, "GET", "/history?room_id=1&limit=10")
            texts = [m["text"] for m in page["messages"]]
            self.assertIn("重启前第一条", texts)
            self.assertIn("重启前第二条", texts)

            resp = join(self.PORT, b, "持久乙")
            self.assertGreaterEqual(resp["read_id"], m1)

            _, d3 = send(self.PORT, a, "持久甲", "重启之后的新消息")
            self.assertGreater(d3["msg"]["id"], m2)

            names = [r["name"] for r in rooms(self.PORT, a)["rooms"]]
            self.assertIn("重启房间", names)
        finally:
            server.stop()
            tmp.cleanup()


# ---------------------------------------------------------------------
# data.json → SQLite 迁移
# ---------------------------------------------------------------------
class TestMigration(unittest.TestCase):
    PORT = 9995

    def test_12_json_to_sqlite(self):
        tmp = tempfile.TemporaryDirectory(prefix="chat-migrate-", ignore_cleanup_errors=True)
        db_file = Path(tmp.name) / "chat.db"
        legacy = Path(tmp.name) / "data.json"
        now = time.time()
        legacy.write_text(json.dumps({
            "next_id": 5, "next_rev": 9,
            "messages": [
                {"id": 3, "rev": 3, "uuid": "old-uuid-1", "client_id": "old-c",
                 "device_id": "old-dev", "name": "老用户", "text": "迁移前的老消息",
                 "ts": now - 60, "time": "10:00:00", "system": False,
                 "revoked": False, "edited_at": None, "edit_count": 0,
                 "audience": ["dev-other"]},
            ],
            "read_positions": {"old-dev": {"name": "老用户", "read_id": 3}},
        }, ensure_ascii=False), encoding="utf-8")

        server = ServerProcess(self.PORT, db_file, CHAT_IMPORT_JSON=str(legacy))
        try:
            server.start()
            status, page = request(self.PORT, "GET", "/history?room_id=1&limit=10")
            self.assertEqual(status, 200)
            self.assertIn("迁移前的老消息", [m["text"] for m in page["messages"]])

            st = state(self.PORT, "")
            self.assertIn("old-dev", st["read_positions"])
            self.assertEqual(st["read_positions"]["old-dev"]["read_id"], 3)

            old_msg = [m for m in page["messages"] if m["text"] == "迁移前的老消息"][0]
            self.assertIn("dev-other", old_msg["audience"])
            self.assertEqual(old_msg["room_id"], 1)

            resp = join(self.PORT, "new-c", "新用户", device="new-dev")
            self.assertGreaterEqual(resp["latest_id"], 4)
            _, d = send(self.PORT, "new-c", "新用户", "迁移后的新消息")
            self.assertGreater(d["msg"]["id"], 4)

            self.assertTrue(Path(str(legacy) + ".migrated").exists())
            self.assertFalse(legacy.exists())
        finally:
            server.stop()
            tmp.cleanup()


# ---------------------------------------------------------------------
# v3：多房间
# ---------------------------------------------------------------------
class TestRooms(unittest.TestCase):
    PORT = 9994

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="chat-rooms-", ignore_cleanup_errors=True)
        cls.server = ServerProcess(cls.PORT, Path(cls.tmp.name) / "chat.db")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_13_room_create_and_list(self):
        a = uid("a")
        join(self.PORT, a, "建房甲")

        status, data = request(self.PORT, "POST", "/rooms", {
            "client_id": a, "name": "学习",
        })
        self.assertEqual(status, 200, data)
        room_id = data["room"]["id"]
        self.assertGreater(room_id, 1)

        names = [r["name"] for r in data["rooms"]]
        self.assertEqual(names[0], "大厅", "大厅永远排第一")
        self.assertIn("学习", names)

        status, data = request(self.PORT, "POST", "/rooms", {
            "client_id": a, "name": "学习",
        })
        self.assertEqual(status, 409)
        self.assertIn("同名", data["error"])

        status, data = request(self.PORT, "POST", "/rooms", {
            "client_id": a, "name": "  ",
        })
        self.assertEqual(status, 400)

    def test_14_room_isolation(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "隔离甲")
        _, created = request(self.PORT, "POST", "/rooms", {"client_id": a, "name": "隔离房"})
        room2 = created["room"]["id"]
        join(self.PORT, b, "隔离乙", room_id=room2)

        _, d1 = send(self.PORT, a, "隔离甲", "大厅里的消息")
        _, d2 = send(self.PORT, b, "隔离乙", "隔离房里的消息", room_id=room2)

        st1 = state(self.PORT, a)
        texts = [m["text"] for m in st1["messages"]]
        self.assertIn("大厅里的消息", texts)
        self.assertNotIn("隔离房里的消息", texts)

        st2 = state(self.PORT, b, room_id=room2)
        texts2 = [m["text"] for m in st2["messages"]]
        self.assertIn("隔离房里的消息", texts2)
        self.assertNotIn("大厅里的消息", texts2)

        status, hist2 = request(self.PORT, "GET", f"/history?room_id={room2}")
        self.assertNotIn("大厅里的消息", [m["text"] for m in hist2["messages"]])

        # 已读分母只算同房间的人
        self.assertNotIn("dev-" + a, d2["msg"]["audience"])

    def test_16_room_scoped_read(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "读甲")
        join(self.PORT, b, "读乙")
        _, created = request(self.PORT, "POST", "/rooms", {"client_id": a, "name": "读房"})
        room2 = created["room"]["id"]
        join(self.PORT, b, "读乙", room_id=room2)

        _, d = send(self.PORT, a, "读甲", "大厅消息2")
        msg_id = d["msg"]["id"]

        # B 在大厅里报已读，不能影响它在读房的水位
        request(self.PORT, "POST", "/read", {"client_id": b, "read_id": msg_id, "room_id": 1})
        st_room2 = state(self.PORT, b, room_id=room2)
        self.assertLess(st_room2["my_read_id"], msg_id)
        st_room1 = state(self.PORT, b, room_id=1)
        self.assertGreaterEqual(st_room1["my_read_id"], msg_id)


# ---------------------------------------------------------------------
# v3：房间未读 / 在线（独立实例，避免其他用例的在线状态干扰）
# ---------------------------------------------------------------------
class TestRoomStats(unittest.TestCase):
    PORT = 9991

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="chat-stats-", ignore_cleanup_errors=True)
        cls.server = ServerProcess(cls.PORT, Path(cls.tmp.name) / "chat.db")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_15_room_unread_and_online(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "未读甲")
        join(self.PORT, b, "未读乙")
        _, created = request(self.PORT, "POST", "/rooms", {"client_id": a, "name": "未读房"})
        room2 = created["room"]["id"]

        # B 切到新房间（/state 会更新它当前所在房间）
        state(self.PORT, b, room_id=room2)
        send(self.PORT, a, "未读甲", "大厅里的未读消息")

        by_name = {r["name"]: r for r in rooms(self.PORT, b)["rooms"]}
        self.assertEqual(by_name["大厅"]["unread"], 1)
        self.assertEqual(by_name["未读房"]["unread"], 0)
        self.assertEqual(by_name["大厅"]["online"], 1, "A 在大厅")
        self.assertEqual(by_name["未读房"]["online"], 1, "B 在未读房")
        self.assertEqual(by_name["大厅"]["last_text"], "大厅里的未读消息")


# ---------------------------------------------------------------------
# v3：头像 / 图片
# ---------------------------------------------------------------------
class TestMedia(unittest.TestCase):
    PORT = 9993

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="chat-media-", ignore_cleanup_errors=True)
        cls.server = ServerProcess(cls.PORT, Path(cls.tmp.name) / "chat.db")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def test_17_avatar_upload_and_fetch(self):
        a = uid("a")
        join(self.PORT, a, "头像甲")

        status, data = request(self.PORT, "POST", "/avatar", {
            "client_id": a, "data_url": PNG_1PX_URL,
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["version"], 1)
        self.assertIn("dev-" + a, data["avatars"])

        status, ctype, body = fetch_bytes(
            self.PORT, f"/avatar?device_id=dev-{a}&v=1")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, PNG_1PX)

        # 再传一次，版本号必须增长，前端才能靠它刷缓存
        status, data2 = request(self.PORT, "POST", "/avatar", {
            "client_id": a, "data_url": PNG_1PX_URL,
        })
        self.assertEqual(status, 200)
        self.assertEqual(data2["version"], 2)

        # /state 里能看到头像版本
        st = state(self.PORT, a)
        self.assertEqual(st["avatars"]["dev-" + a], 2)

    def test_18_avatar_rejections(self):
        a = uid("a")
        join(self.PORT, a, "头像乙")

        status, data = request(self.PORT, "POST", "/avatar", {
            "client_id": a, "data_url": "data:text/plain;base64,aGVsbG8=",
        })
        self.assertEqual(status, 400)

        # PNG 的声明 + JPEG 的内容 → 内容与格式不符
        fake = "data:image/png;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * 100).decode()
        status, data = request(self.PORT, "POST", "/avatar", {
            "client_id": a, "data_url": fake,
        })
        self.assertEqual(status, 400)
        self.assertIn("不符", data["error"])

        big = "data:image/png;base64," + base64.b64encode(PNG_1PX + b"0" * 250_000).decode()
        status, data = request(self.PORT, "POST", "/avatar", {
            "client_id": a, "data_url": big,
        })
        self.assertEqual(status, 400)
        self.assertIn("图片太大", data["error"])

    def test_19_image_message(self):
        a, b = uid("a"), uid("b")
        join(self.PORT, a, "图片甲")
        join(self.PORT, b, "图片乙")

        status, data = send_image(self.PORT, a, "图片甲")
        self.assertEqual(status, 200, data)
        msg = data["msg"]
        self.assertEqual(msg["kind"], "image")
        self.assertEqual(msg["text"], "")

        status, ctype, body = fetch_bytes(
            self.PORT, f"/image?message_id={msg['id']}&v={msg['rev']}")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, PNG_1PX)

        st = state(self.PORT, b)
        mine = [m for m in st["messages"] if m["id"] == msg["id"]][0]
        self.assertEqual(mine["kind"], "image")

        # 图片消息不能编辑
        status, data = request(self.PORT, "POST", "/edit", {
            "client_id": a, "id": msg["id"], "text": "改文字",
        })
        self.assertEqual(status, 400)
        self.assertIn("图片消息不能编辑", data["error"])

        # 撤回后图片字节应该一起删掉
        status, data = request(self.PORT, "POST", "/revoke", {"client_id": a, "id": msg["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["msg"]["revoked"])
        status, _, _ = fetch_bytes(self.PORT, f"/image?message_id={msg['id']}")
        self.assertEqual(status, 404)

    def test_20_image_rejections(self):
        a = uid("a")
        join(self.PORT, a, "图片丙")

        status, data = send_image(self.PORT, a, "图片丙",
                                  data_url="data:image/png;base64,aGVsbG8=")
        self.assertEqual(status, 400)

        status, data = send_image(self.PORT, a, "图片丙",
                                  data_url="data:text/plain;base64,aGVsbG8=")
        self.assertEqual(status, 400)

        big = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"0" * 1_600_000).decode()
        status, data = send_image(self.PORT, a, "图片丙", data_url=big)
        self.assertEqual(status, 400)
        self.assertIn("图片太大", data["error"])


# ---------------------------------------------------------------------
# v3：v1 表结构 → v2 自动迁移
# ---------------------------------------------------------------------
class TestSchemaV2Migration(unittest.TestCase):
    PORT = 9992

    def test_21_v1_schema_upgrade(self):
        tmp = tempfile.TemporaryDirectory(prefix="chat-v1-", ignore_cleanup_errors=True)
        db_file = Path(tmp.name) / "chat.db"
        now = time.time()

        conn = sqlite3.connect(db_file)
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, rev INTEGER NOT NULL, uuid TEXT,
                client_id TEXT NOT NULL, device_id TEXT NOT NULL, name TEXT NOT NULL,
                text TEXT NOT NULL, ts REAL NOT NULL, time TEXT NOT NULL,
                system INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                revoked_at REAL, edited_at REAL, edit_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE message_audience (
                message_id INTEGER NOT NULL, device_id TEXT NOT NULL,
                PRIMARY KEY (message_id, device_id));
            CREATE TABLE read_positions (
                device_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                read_id INTEGER NOT NULL DEFAULT 0);
            INSERT INTO messages(id, rev, uuid, client_id, device_id, name, text, ts, time)
                VALUES (5, 7, 'v1-uuid', 'v1-c', 'v1-dev', '老用户', 'v1 时代的老消息', 0, '09:00:00');
            INSERT INTO read_positions(device_id, name, read_id) VALUES ('v1-dev', '老用户', 5);
            PRAGMA user_version = 0;
        """)
        conn.execute("UPDATE messages SET ts = ?", (now - 100,))
        conn.commit()
        conn.close()

        server = ServerProcess(self.PORT, db_file)
        try:
            server.start()

            status, page = request(self.PORT, "GET", "/history?room_id=1")
            old = [m for m in page["messages"] if m["text"] == "v1 时代的老消息"]
            self.assertEqual(len(old), 1)
            self.assertEqual(old[0]["room_id"], 1)

            st = state(self.PORT, "")
            self.assertEqual(st["read_positions"]["v1-dev"]["read_id"], 5)

            names = [r["name"] for r in rooms(self.PORT)["rooms"]]
            self.assertEqual(names[0], "大厅")

            conn = sqlite3.connect(db_file)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
            self.assertIn("room_id", cols)
            self.assertIn("kind", cols)
            rp_cols = [r[1] for r in conn.execute("PRAGMA table_info(read_positions)")]
            self.assertIn("room_id", rp_cols)
            conn.close()

            # 旧消息之后的新消息编号接着走
            join(self.PORT, "new-c", "新用户", device="new-dev")
            _, d = send(self.PORT, "new-c", "新用户", "升级后的新消息")
            self.assertGreater(d["msg"]["id"], 5)
        finally:
            server.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
