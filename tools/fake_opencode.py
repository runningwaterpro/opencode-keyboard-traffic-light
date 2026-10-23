#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假的 OpenCode V2 服务器桩，供守护进程 dry-run 冒烟测试使用。

用法：
    python tools/fake_opencode.py --port 28998 --service-json <路径>

行为：
- 启动时写 service.json，再监听 127.0.0.1:<port>（线程化，SSE 长连接期间 REST 仍可响应）。
- 所有 /api/* 路由需要 Basic 认证（opencode:smoke-secret），不符返回 401。
- GET /api/event 是 SSE 事件流：第 1 次连接按相位 0 脚本发事件，+2.0s 主动断开（模拟重启）；
  第 2 次及以后按相位 1 脚本发事件并保持心跳，直到客户端断开。
- 相位 1 时 REST 返回已建好的会话 A/B，供守护进程重连后重建状态。
"""
import argparse
import base64
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.1.3"
AUTH = "Basic " + base64.b64encode(b"opencode:smoke-secret").decode()
STUB_DIR = "D:\\dev\\opencode-stub"  # 会话 location.directory 的统一值

_lock = threading.Lock()
_phase = 0      # 0：首次连接阶段；1：重连阶段
_sse_count = 0  # SSE 连接计数
_evt_n = 0      # 事件序号


def log(method, path, status):
    """每个请求向 stdout 打一行日志。"""
    print(f"{method} {path} -> {status}", flush=True)


def session_a():
    return {
        "id": "ses_stubA", "projectID": "prj_stub", "agent": "build",
        "model": {"id": "stub", "providerID": "stub"},
        "cost": 0, "tokens": {"input": 0, "output": 0},
        "outcome": "succeeded",
        "time": {"created": 1792700000000, "updated": 1792700001000},
        "title": "冒烟会话A",
        "location": {"directory": STUB_DIR},
    }


def session_b():
    """会话 B：title 故意缺省，守护进程应走详情兜底路径。"""
    return {
        "id": "ses_stubB", "projectID": "prj_stub", "agent": "build",
        "model": {"id": "stub", "providerID": "stub"},
        "cost": 0, "tokens": {"input": 0, "output": 0},
        "time": {"created": 1792700002000, "updated": 1792700003000},
        "location": {"directory": STUB_DIR},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 屏蔽默认请求日志，统一走 log()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        log(self.command, self.path, status)

    def do_GET(self):
        path = self.path.split("?", 1)[0]  # 忽略查询串（limit/order/archived/cursor 等）
        if path.startswith("/api/") and self.headers.get("Authorization") != AUTH:
            self._json({"error": "unauthorized"}, 401)
            return
        with _lock:
            phase = _phase
        if path == "/api/info":
            port = self.server.server_address[1]
            self._json({"version": VERSION, "pid": os.getpid(),
                        "urls": [f"http://127.0.0.1:{port}"],
                        "paths": {}, "capabilities": {}})
        elif path == "/api/event":
            self._sse()
        elif path == "/api/session":
            data = [] if phase == 0 else [session_a(), session_b()]
            self._json({"data": data, "cursor": {"next": None}})
        elif path == "/api/session/active":
            data = {} if phase == 0 else {"ses_stubB": {"type": "running"}}
            self._json({"data": data})
        elif path == "/api/permission/request":
            self._json({"data": []})
        elif path == "/api/form":
            self._json({"data": []})
        elif path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            if phase == 0:
                self._json({"error": "session not found"}, 404)
            elif sid == "ses_stubA":
                self._json({"data": session_a()})
            elif sid == "ses_stubB":
                obj = session_b()
                obj["title"] = "冒烟会话B"  # 详情兜底时补上 title
                self._json({"data": obj})
            else:
                self._json({"error": "session not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def _not_found(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/") and self.headers.get("Authorization") != AUTH:
            self._json({"error": "unauthorized"}, 401)
        else:
            self._json({"error": "not found"}, 404)

    do_POST = _not_found
    do_PUT = _not_found
    do_DELETE = _not_found
    do_PATCH = _not_found

    # ---------- SSE ----------

    def _sleep_until(self, deadline):
        delay = deadline - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _emit(self, etype, data):
        global _evt_n
        with _lock:
            _evt_n += 1
            n = _evt_n
        frame = {"id": f"evt_{n}", "created": int(time.time() * 1000),
                 "type": etype, "data": data}
        payload = "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _sse(self):
        global _phase, _sse_count
        with _lock:
            _sse_count += 1
            conn_no = _sse_count
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        log(self.command, self.path, 200)
        t0 = time.monotonic()
        try:
            if conn_no == 1:
                self._script_phase0(t0)  # 第 1 次连接：相位 0 脚本，+2.0s 后断开
            else:
                self._script_phase1(t0)  # 第 2 次及以后：相位 1 脚本 + 心跳，保持连接
        except OSError:
            pass  # 客户端断开（BrokenPipe/ConnectionReset 等），直接吞掉
        finally:
            self.close_connection = True
        if conn_no == 1:
            with _lock:
                _phase = 1  # 先切相位、后关连接，客户端立刻重连时看到的就是相位 1

    def _script_phase0(self, t0):
        script = [
            (0.2, "server.connected", {}),
            (0.4, "session.created", {"sessionID": "ses_stubA",
                                      "location": {"directory": STUB_DIR}}),
            (0.6, "session.renamed", {"sessionID": "ses_stubA", "title": "冒烟会话A"}),
            (0.8, "session.execution.started", {"sessionID": "ses_stubA"}),
            (1.1, "form.created", {"form": {"id": "frm_stub1", "sessionID": "ses_stubA",
                                            "title": "冒烟提问",
                                            "metadata": {"kind": "question"},
                                            "fields": []}}),
            (1.4, "form.cancelled", {"id": "frm_stub1", "sessionID": "ses_stubA"}),
            (1.7, "session.execution.succeeded", {"sessionID": "ses_stubA"}),
        ]
        for at, etype, data in script:
            self._sleep_until(t0 + at)
            self._emit(etype, data)
        self._sleep_until(t0 + 2.0)  # 到点后返回 => 连接被干净关闭

    def _script_phase1(self, t0):
        self._sleep_until(t0 + 0.2)
        self._emit("server.connected", {})
        self._sleep_until(t0 + 0.4)
        self._emit("session.created", {"sessionID": "ses_stubB",
                                       "location": {"directory": STUB_DIR}})
        self._sleep_until(t0 + 0.7)
        self._emit("session.execution.started", {"sessionID": "ses_stubB"})
        while True:  # 保持连接，每 5 秒一次心跳，直到客户端断开
            time.sleep(5)
            self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()


def main():
    ap = argparse.ArgumentParser(description="假 OpenCode V2 服务器桩（冒烟测试）")
    ap.add_argument("--port", type=int, default=28998)
    ap.add_argument("--service-json", required=True, help="service.json 的输出路径")
    args = ap.parse_args()

    info = {"id": str(uuid.uuid4()), "version": VERSION,
            "url": f"http://127.0.0.1:{args.port}", "pid": os.getpid(),
            "password": "smoke-secret"}
    svc_dir = os.path.dirname(os.path.abspath(args.service_json))
    os.makedirs(svc_dir, exist_ok=True)  # 目录不存在先建
    with open(args.service_json, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    print(f"service.json written -> {args.service_json}", flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"fake opencode v{VERSION} listening on http://127.0.0.1:{args.port}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()