"""服务发现（纯逻辑）：读 service.json、构造 Basic 认证头、校验服务一致性。

只做本地文件读取与数据校验，不发任何网络请求；
/api/info 的抓取由调用方完成，本模块只接收它的 JSON dict。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SERVICE_JSON: Path = Path.home() / ".local" / "state" / "opencode" / "service.json"
BASIC_USER = "opencode"

_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class ServiceInfo:
    """service.json 的已校验内容。"""

    id: str
    version: str
    url: str
    pid: int
    password: str


class ServiceFileError(Exception):
    """service.json 格式问题：JSON 坏、字段缺失或类型不对、url 非法。"""


class StaleServiceError(Exception):
    """服务可达，但 /api/info 与 service.json 对不上（文件过期）。"""


def _nonempty_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ServiceFileError(f"字段 {key!r} 必须是非空字符串")
    return value


def _positive_pid(data: dict) -> int:
    value = data.get("pid")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ServiceFileError(f"字段 'pid' 必须是正整数，实际为 {value!r}")
    return value


def _local_url(data: dict) -> str:
    value = data.get("url")
    if not isinstance(value, str) or not value:
        raise ServiceFileError(f"字段 'url' 必须是非空字符串，实际为 {value!r}")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ServiceFileError(f"url 无法解析：{value!r}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ServiceFileError(f"url scheme 必须是 http/https：{value!r}")
    if parsed.hostname not in _LOCAL_HOSTNAMES:
        raise ServiceFileError(f"url 必须指向本机：{value!r}")
    if port is None:
        raise ServiceFileError(f"url 必须带明确端口：{value!r}")
    return value


def load_service_file(path: str | Path) -> ServiceInfo:
    """读取并校验 service.json。

    文件不存在时让 FileNotFoundError 等 OSError 自然抛出；
    内容格式问题统一抛 ServiceFileError。
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ServiceFileError(f"service.json 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ServiceFileError("service.json 顶层必须是 JSON 对象")
    return ServiceInfo(
        id=_nonempty_str(data, "id"),
        version=_nonempty_str(data, "version"),
        url=_local_url(data),
        pid=_positive_pid(data),
        password=_nonempty_str(data, "password"),
    )


def basic_auth_header(password: str) -> str:
    """构造 HTTP Basic 认证头：用户名固定 opencode。"""
    token = base64.b64encode(f"{BASIC_USER}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _normalize_url(url: str) -> str:
    """比较用归一化：去掉尾部 '/'。"""
    return url.rstrip("/")


def check_consistency(info: ServiceInfo, payload: dict) -> None:
    """校验 /api/info 的 JSON dict 与 service.json 一致。

    任一项对不上（含字段缺失、类型不对）都视为文件过期，抛 StaleServiceError；
    url 比较前去掉尾部 '/'。一致则返回 None。
    """
    if not isinstance(payload, dict):
        raise StaleServiceError("api/info 返回不是 JSON 对象")
    urls = payload.get("urls")
    if isinstance(urls, list):
        available = {_normalize_url(u) for u in urls if isinstance(u, str)}
    else:
        available = set()
    if _normalize_url(info.url) not in available:
        raise StaleServiceError(f"url 不在 api/info 的 urls 中：{info.url!r} vs {urls!r}")
    if payload.get("version") != info.version:
        raise StaleServiceError(f"version 不一致：{info.version!r} vs {payload.get('version')!r}")
    if payload.get("pid") != info.pid:
        raise StaleServiceError(f"pid 不一致：{info.pid!r} vs {payload.get('pid')!r}")