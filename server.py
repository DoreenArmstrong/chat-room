# -*- coding: utf-8 -*-
"""
局域网聊天室 - 服务器
只用 Python 标准库，不需要安装任何第三方包。

启动方式:
    python server.py   (或双击 启动聊天室.bat)
停止方式:
    在窗口里按 Ctrl + C
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"  # 监听所有网卡，这样局域网内其他设备才能连进来
PORT = 9999

BASE_DIR = Path(__file__).resolve().parent

# ---------------- 聊天室数据（保存在内存里） ----------------
lock = threading.Lock()  # 多人同时发消息时加锁，防止数据错乱
messages = []            # [{'id': 1, 'name': '张三', 'text': '大家好', 'time': '12:30:05'}, ...]
next_id = 1              # 消息自增编号，浏览器靠它区分"哪些是新消息"
MAX_KEEP = 500           # 内存里最多保留最近 500 条


def add_message(name, text, is_system=False):
    """往消息列表里追加一条消息"""
    global next_id
    with lock:
        msg = {
            "id": next_id,
            "name": name,
            "text": text,
            "time": time.strftime("%H:%M:%S"),
            "system": is_system,  # 系统提示，比如"xx 加入了聊天室"
        }
        next_id += 1
        messages.append(msg)
        if len(messages) > MAX_KEEP:
            del messages[: len(messages) - MAX_KEEP]
        return msg


def get_messages_after(after_id):
    """取出编号大于 after_id 的消息（浏览器每次只要新的那部分）"""
    with lock:
        return [m for m in messages if m["id"] > after_id]


class ChatHandler(BaseHTTPRequestHandler):
    """处理浏览器发来的每一个 HTTP 请求"""

    # ---------- 小工具：返回 JSON / 返回文件 ----------
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            body = Path(path).read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ---------- GET 请求：打开网页 / 拉取新消息 ----------
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_file(BASE_DIR / "index.html", "text/html; charset=utf-8")
        elif url.path == "/messages":
            after = int(parse_qs(url.query).get("after", ["0"])[0])
            self.send_json({"messages": get_messages_after(after)})
        else:
            self.send_error(404)

    # ---------- POST 请求：发消息 / 进入聊天室 ----------
    def do_POST(self):
        url = urlparse(self.path)
        try:
            data = self.read_body_json()
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "请求体不是合法的 JSON"}, 400)
            return

        if url.path == "/send":
            name = str(data.get("name", "匿名")).strip()[:20] or "匿名"
            text = str(data.get("text", "")).strip()[:2000]
            if not text:
                self.send_json({"ok": False, "error": "消息不能为空"}, 400)
                return
            msg = add_message(name, text)
            print(f"[{msg['time']}] {name}: {text}")
            self.send_json({"ok": True, "id": msg["id"]})

        elif url.path == "/join":
            name = str(data.get("name", "匿名")).strip()[:20] or "匿名"
            msg = add_message("", f"{name} 加入了聊天室", is_system=True)
            print(f"[{msg['time']}] {name} 加入了聊天室")
            self.send_json({"ok": True})

        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        # 控制台日志简化成：时间 + 对方IP + 请求内容
        print(time.strftime("[%H:%M:%S]"), self.address_string(), fmt % args)


def main():
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    print("=" * 50)
    print("  聊天室服务器已启动！")
    print(f"  自己电脑访问:   http://127.0.0.1:{PORT}")
    print(f"  其他人访问:     http://<你的局域网IP>:{PORT}")
    print(f"  (查看本机IP: 打开命令行输入 ipconfig，看 IPv4 地址)")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")


if __name__ == "__main__":
    main()
