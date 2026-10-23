"""服务发现测试：全部离线；service.json 夹具用 tmp_path 现场写。"""
import base64
import json
from pathlib import Path

import pytest

from traffic_light.discovery import (
    DEFAULT_SERVICE_JSON,
    ServiceFileError,
    ServiceInfo,
    StaleServiceError,
    basic_auth_header,
    check_consistency,
    load_service_file,
)

VALID = {
    "id": "20ffbb0a-bee9-445e-8def-a9b62ba10a4e",
    "version": "2.1.3",
    "url": "http://127.0.0.1:28894",
    "pid": 25276,
    "password": "VS8bRUy6NjNytcCLgUXWgo2mdNHz5pkuN9tWTpzldj8",
}

INFO = {
    "version": "2.1.3",
    "pid": 25276,
    "urls": ["http://127.0.0.1:28894"],
    "paths": {},
    "capabilities": {},
}


def write_service(tmp_path: Path, payload) -> Path:
    """payload 是 dict 就按 JSON 序列化，是 str 就原样写入。"""
    path = tmp_path / "service.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def load(tmp_path: Path, payload) -> ServiceInfo:
    return load_service_file(write_service(tmp_path, payload))


def make_service(**overrides) -> ServiceInfo:
    return ServiceInfo(**{**VALID, **overrides})


def make_info(**overrides) -> dict:
    return {**INFO, **overrides}


def test_default_path_is_under_home():
    assert DEFAULT_SERVICE_JSON == Path.home() / ".local" / "state" / "opencode" / "service.json"


def test_load_valid_file(tmp_path):
    svc = load(tmp_path, VALID)
    assert isinstance(svc, ServiceInfo)
    assert svc.id == VALID["id"]
    assert svc.version == VALID["version"]
    assert svc.url == VALID["url"]
    assert svc.pid == VALID["pid"]
    assert svc.password == VALID["password"]


@pytest.mark.parametrize("url", ["http://localhost:28894", "http://[::1]:28894", "https://127.0.0.1:443"])
def test_load_accepts_local_urls(tmp_path, url):
    assert load(tmp_path, {**VALID, "url": url}).url == url


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_service_file(tmp_path / "nope" / "service.json")


@pytest.mark.parametrize("bad", ["{ broken", "", "not json"])
def test_broken_json_raises(tmp_path, bad):
    with pytest.raises(ServiceFileError):
        load(tmp_path, bad)


@pytest.mark.parametrize("bad", ["[1, 2, 3]", '"text"', "42", "null"])
def test_top_level_not_object_raises(tmp_path, bad):
    with pytest.raises(ServiceFileError):
        load(tmp_path, bad)


@pytest.mark.parametrize("key", ["id", "version", "url", "pid", "password"])
def test_missing_field_raises(tmp_path, key):
    with pytest.raises(ServiceFileError):
        load(tmp_path, {k: v for k, v in VALID.items() if k != key})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", ""),
        ("id", 7),
        ("version", ""),
        ("version", None),
        ("password", ""),
        ("pid", "25276"),
        ("pid", True),
        ("pid", 0),
        ("pid", -5),
        ("pid", 25276.0),
    ],
)
def test_field_type_or_value_raises(tmp_path, key, value):
    with pytest.raises(ServiceFileError):
        load(tmp_path, {**VALID, key: value})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",            # 缺端口
        "http://example.com:28894",    # 非本机主机
        "ftp://127.0.0.1:28894",       # scheme 非 http/https
        "ws://127.0.0.1:28894",        # scheme 非 http/https
        "127.0.0.1:28894",             # 缺 scheme
        "http://127.0.0.1:notaport",   # 端口非数字
        "http://127.0.0.1:70000",      # 端口越界
        "",
    ],
)
def test_bad_url_raises(tmp_path, url):
    with pytest.raises(ServiceFileError):
        load(tmp_path, {**VALID, "url": url})


def test_consistency_passes():
    check_consistency(make_service(), make_info())


def test_consistency_passes_with_trailing_slash():
    check_consistency(make_service(url=VALID["url"] + "/"), make_info())
    check_consistency(make_service(), make_info(urls=[VALID["url"] + "/"]))


def test_consistency_version_mismatch_is_stale():
    with pytest.raises(StaleServiceError):
        check_consistency(make_service(), make_info(version="2.1.4"))


def test_consistency_pid_mismatch_is_stale():
    with pytest.raises(StaleServiceError):
        check_consistency(make_service(), make_info(pid=1))


@pytest.mark.parametrize("urls", [[], ["http://127.0.0.1:28895"], ["http://localhost:28894"]])
def test_consistency_url_not_listed_is_stale(urls):
    with pytest.raises(StaleServiceError):
        check_consistency(make_service(), make_info(urls=urls))


@pytest.mark.parametrize(
    "payload",
    [{}, {"urls": INFO["urls"]}, {"urls": INFO["urls"], "version": INFO["version"]}, []],
)
def test_consistency_unverifiable_payload_is_stale(payload):
    with pytest.raises(StaleServiceError):
        check_consistency(make_service(), payload)


def test_basic_auth_header_known_password():
    assert basic_auth_header("secret") == "Basic b3BlbmNvZGU6c2VjcmV0"


def test_basic_auth_header_roundtrip():
    raw = base64.b64encode(f"opencode:{VALID['password']}".encode("utf-8")).decode("ascii")
    assert basic_auth_header(VALID["password"]) == f"Basic {raw}"