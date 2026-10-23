"""REST 客户端与快照重建：抓取 OpenCode 服务接口并组装完整 Snapshot。

网络仅用标准库 urllib，认证用 discovery.basic_auth_header（用户名固定 opencode）。
build_snapshot 只做纯组装，client 满足鸭子类型即可，可完全离线测试。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .core import SessionSnapshot, Snapshot
from .discovery import ServiceInfo, basic_auth_header

MAX_SESSION_COUNT = 5000  # 安全阀：翻页累计超过此数即停止，all_ids 置 None（列表不完整）


def _request_json(url: str, password: str, timeout: float) -> dict:
    """发带 Basic 认证的 GET 请求；网络/解析错误向上抛。"""
    req = urllib.request.Request(url, headers={"Authorization": basic_auth_header(password)})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def fetch_info(info: ServiceInfo, timeout: float = 10.0) -> dict:
    """GET {info.url}/api/info（Basic 认证），返回 JSON dict；网络/解析错误向上抛。"""
    return _request_json(info.url.rstrip("/") + "/api/info", info.password, timeout)


def _data_list(payload) -> list:
    """取响应里的 data；不是列表就返回 []。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


def _next_cursor(page) -> str | None:
    """取一页响应的 cursor.next；只有非空字符串才算还有下一页。"""
    cursor = page.get("cursor") if isinstance(page, dict) else None
    nxt = cursor.get("next") if isinstance(cursor, dict) else None
    return nxt if isinstance(nxt, str) and nxt else None


def _text(value) -> str | None:
    """非空字符串保留，其余归一化为 None。"""
    return value if isinstance(value, str) and value else None


class RestClient:
    """单个 OpenCode 服务的 REST 客户端；方法返回原始 JSON，不做业务解释。"""

    def __init__(self, info: ServiceInfo, timeout: float = 15.0):
        self.info = info
        self.timeout = timeout
        self._base = info.url.rstrip("/")
        self._password = info.password

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request_json(url, self._password, self.timeout)

    def sessions_page(self, cursor: str | None = None, limit: int = 500) -> dict:
        """GET /api/session 一页（倒序、不含归档）；cursor 非空时带上。"""
        params: dict = {"limit": limit, "order": "desc", "archived": "false"}
        if cursor:
            params["cursor"] = cursor
        return self._get("/api/session", params)

    def active(self) -> dict:
        """GET /api/session/active，返回原始 JSON。"""
        return self._get("/api/session/active")

    def pending_permissions(self) -> list:
        """GET /api/permission/request，返回 data 列表（缺失时为空列表）。"""
        return _data_list(self._get("/api/permission/request"))

    def pending_forms(self) -> list:
        """GET /api/form，返回 data 列表（缺失时为空列表）。"""
        return _data_list(self._get("/api/form"))

    def session_detail(self, session_id: str) -> dict:
        """GET /api/session/<id>，返回 data 对象；未知 id 时 urlopen 抛 HTTPError(404)。"""
        path = "/api/session/" + urllib.parse.quote(session_id, safe="")
        return self._get(path)["data"]


def _valid_id(item) -> str | None:
    """条目里的 id：必须是非空字符串，否则条目无效。"""
    sid = item.get("id") if isinstance(item, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _running_ids(payload) -> set[str]:
    """active 响应的 data 键集合即运行中会话。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return set()
    return {k for k in data if isinstance(k, str) and k}


def _group_pending(client) -> dict[str, dict[str, str]]:
    """把 permission/form 待处理按 sessionID 归组：{sid: {request_id: 种类}}。"""
    groups: dict[str, dict[str, str]] = {}
    sources = (
        ("permission", client.pending_permissions()),
        ("form", client.pending_forms()),
    )
    for kind, items in sources:
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict):
                continue
            sid, rid = item.get("sessionID"), item.get("id")
            if isinstance(sid, str) and sid and isinstance(rid, str) and rid:
                groups.setdefault(sid, {})[rid] = kind
    return groups


def _directory(item: dict) -> str | None:
    loc = item.get("location")
    return _text(loc.get("directory")) if isinstance(loc, dict) else None


def build_snapshot(client) -> Snapshot:
    """全量重建快照（纯组装）：翻页拉全量，叠加 active/pending，竞态兜底补条目。"""
    items: list[dict] = []
    list_ids: set[str] = set()
    exceeded = False
    cursor: str | None = None
    while True:
        page = client.sessions_page(cursor)
        for raw in _data_list(page):
            sid = _valid_id(raw)
            if sid is None:
                continue
            items.append(raw)
            list_ids.add(sid)
        if len(items) > MAX_SESSION_COUNT:
            exceeded = True  # 列表不完整：主程序跳过缺席清理
            break
        cursor = _next_cursor(page)
        if cursor is None:
            break
    running = _running_ids(client.active())
    pending_group = _group_pending(client)
    sessions: list[SessionSnapshot] = []
    for raw in items:
        sid = raw["id"]
        sessions.append(
            SessionSnapshot(
                session_id=sid,
                parent_id=_text(raw.get("parentID")),
                title=_text(raw.get("title")),
                directory=_directory(raw),
                running=sid in running,
                failed=raw.get("outcome") == "failed" and sid not in running,
                pending=pending_group.pop(sid, {}),
                outcome=_text(raw.get("outcome")),
            )
        )
    # 竞态兜底：不在列表但仍在跑/待处理的会话，补一条最简条目
    for sid in list(running) + [k for k in pending_group if k not in running]:
        if sid in list_ids:
            continue
        sessions.append(
            SessionSnapshot(
                session_id=sid,
                running=sid in running,
                pending=pending_group.pop(sid, {}),
            )
        )
    return Snapshot(sessions=sessions, all_ids=None if exceeded else set(list_ids))
