"""REST 快照重建测试：全部离线。

build_snapshot 用假 client（鸭子类型）验证；RestClient 与 fetch_info 通过
monkeypatch urllib.request.urlopen 验证请求拼装，不发任何真实网络请求。
"""
from __future__ import annotations

import json
import urllib.request

from traffic_light.core import Snapshot
from traffic_light.discovery import ServiceInfo, basic_auth_header
from traffic_light.rest import RestClient, build_snapshot, fetch_info

PW = "pw-test"
BASE = "http://127.0.0.1:28894"


def info() -> ServiceInfo:
    return ServiceInfo(id="test-id", version="2.1.3", url=BASE, pid=123, password=PW)


def sess(sid: str, **overrides) -> dict:
    """构造一条最小 Session.Info 条目。"""
    item = {"id": sid, "location": {"directory": "D:/proj"}}
    item.update(overrides)
    return item


def page(data, next_cursor=None) -> dict:
    """构造 /api/session 一页响应。"""
    return {"data": data, "cursor": {"next": next_cursor}}


class FakeClient:
    """鸭子类型假客户端：按脚本返回预置数据，并记录翻页用的 cursor。"""

    def __init__(self, pages=None, active=None, permissions=None, forms=None):
        self._pages = list(pages or [])
        self._active = dict(active or {})
        self._permissions = list(permissions or [])
        self._forms = list(forms or [])
        self.cursors: list = []

    def sessions_page(self, cursor=None, limit=500):
        self.cursors.append(cursor)
        if self._pages:
            return self._pages.pop(0)
        return page([], None)

    def active(self):
        return {"data": self._active}

    def pending_permissions(self):
        return self._permissions

    def pending_forms(self):
        return self._forms

    def session_detail(self, session_id):
        return {"id": session_id}


def by_id(snap: Snapshot) -> dict:
    return {s.session_id: s for s in snap.sessions}


def test_single_page_derives_states_and_keeps_idle_history():
    client = FakeClient(
        pages=[page([sess("ses_a"), sess("ses_b"), sess("ses_c", outcome="failed"),
                     sess("ses_d", outcome="succeeded")], None)],
        active={"ses_a": {"type": "running"}},
        permissions=[{"id": "prq_1", "sessionID": "ses_b"}],
    )
    snap = build_snapshot(client)
    assert isinstance(snap, Snapshot)
    got = by_id(snap)
    assert set(got) == {"ses_a", "ses_b", "ses_c", "ses_d"}  # idle 历史会话也在
    assert (got["ses_a"].running, got["ses_a"].failed) == (True, False)
    assert got["ses_b"].pending == {"prq_1": "permission"}
    assert (got["ses_b"].running, got["ses_b"].failed) == (False, False)
    assert (got["ses_c"].running, got["ses_c"].failed) == (False, True)
    assert (got["ses_d"].running, got["ses_d"].failed) == (False, False)
    assert snap.all_ids == {"ses_a", "ses_b", "ses_c", "ses_d"}


def test_paging_follows_cursor_next():
    client = FakeClient(pages=[page([sess("ses_1")], "tok1"), page([sess("ses_2")], None)])
    snap = build_snapshot(client)
    assert [s.session_id for s in snap.sessions] == ["ses_1", "ses_2"]
    assert client.cursors == [None, "tok1"]
    assert snap.all_ids == {"ses_1", "ses_2"}


def test_safety_valve_over_5000_makes_all_ids_none():
    items = [sess(f"ses_{i:05d}") for i in range(5001)]
    client = FakeClient(pages=[page(items, "tok1"), page([sess("ses_never")], None)])
    snap = build_snapshot(client)
    assert snap.all_ids is None
    assert len(snap.sessions) == 5001
    assert client.cursors == [None]  # 超过 5000 后不再翻页


def test_safety_valve_boundary_exactly_5000_is_complete():
    items = [sess(f"ses_{i:05d}") for i in range(5000)]
    client = FakeClient(pages=[page(items, "tok1"), page([], None)])
    snap = build_snapshot(client)
    assert len(snap.sessions) == 5000
    assert snap.all_ids is not None and len(snap.all_ids) == 5000
    assert client.cursors == [None, "tok1"]  # 恰好 5000 时继续翻页


def test_race_fallback_adds_sessions_missing_from_list():
    client = FakeClient(
        pages=[page([sess("ses_known")], None)],
        active={"ses_run_ghost": {"type": "running"}},
        forms=[{"id": "frm_1", "sessionID": "ses_form_ghost"}],
    )
    snap = build_snapshot(client)
    got = by_id(snap)
    ghost = got["ses_run_ghost"]
    assert ghost.running is True and ghost.failed is False and ghost.pending == {}
    assert (ghost.parent_id, ghost.title, ghost.directory) == (None, None, None)
    fghost = got["ses_form_ghost"]
    assert fghost.running is False
    assert fghost.pending == {"frm_1": "form"}
    assert snap.all_ids == {"ses_known"}  # all_ids 只统计列表里出现过的 id


def test_same_session_has_permission_and_form_pending():
    client = FakeClient(
        pages=[page([sess("ses_x")], None)],
        permissions=[{"id": "prq_9", "sessionID": "ses_x"}],
        forms=[{"id": "frm_9", "sessionID": "ses_x"}],
    )
    snap = build_snapshot(client)
    assert snap.sessions[0].pending == {"prq_9": "permission", "frm_9": "form"}


def test_failed_requires_outcome_failed_and_not_running():
    client = FakeClient(
        pages=[page([sess("ses_r", outcome="failed"), sess("ses_n"),
                     sess("ses_f", outcome="interrupted")], None)],
        active={"ses_r": {"type": "running"}},
    )
    snap = build_snapshot(client)
    got = by_id(snap)
    assert (got["ses_r"].running, got["ses_r"].failed) == (True, False)
    assert got["ses_n"].failed is False  # outcome 缺失
    assert got["ses_f"].failed is False  # 只有 "failed" 算失败
    assert got["ses_f"].running is False


def test_passthrough_parent_title_directory():
    item = {"id": "ses_p", "parentID": "ses_parent", "title": "标题",
            "location": {"directory": "C:/work/demo"}}
    snap = build_snapshot(FakeClient(pages=[page([item], None)]))
    s = snap.sessions[0]
    assert s.parent_id == "ses_parent"
    assert s.title == "标题"
    assert s.directory == "C:/work/demo"


def test_parent_title_directory_missing_become_none():
    snap = build_snapshot(FakeClient(pages=[page([{"id": "ses_bare"}], None)]))
    s = snap.sessions[0]
    assert (s.parent_id, s.title, s.directory) == (None, None, None)


def test_non_str_id_entries_are_skipped():
    items = [{"id": 123}, {"id": None}, {"id": ""}, {"no_id": 1}, sess("ses_ok")]
    snap = build_snapshot(FakeClient(pages=[page(items, None)]))
    assert [s.session_id for s in snap.sessions] == ["ses_ok"]
    assert snap.all_ids == {"ses_ok"}


class FakeResponse:
    """urlopen 的最小替身：只需 read 与上下文管理。"""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def patch_urlopen(monkeypatch, calls, payload):
    """把 urllib.request.urlopen 换成假实现，记录 (url, auth, timeout)。"""

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization"), timeout))
        return FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_rest_client_sends_basic_auth_and_paging_params(monkeypatch):
    calls = []
    patch_urlopen(monkeypatch, calls, {"data": [], "cursor": {"next": None}})
    client = RestClient(info(), timeout=3.0)
    client.sessions_page(cursor="tok")
    url, auth, timeout = calls[0]
    assert url.startswith(BASE + "/api/session?")
    for frag in ("limit=500", "order=desc", "archived=false", "cursor=tok"):
        assert frag in url
    assert auth == basic_auth_header(PW)
    assert timeout == 3.0


def test_rest_client_without_cursor_omits_cursor_param(monkeypatch):
    calls = []
    patch_urlopen(monkeypatch, calls, {"data": [], "cursor": {"next": None}})
    RestClient(info()).sessions_page()
    assert "cursor" not in calls[0][0]
    assert calls[0][0] == BASE + "/api/session?limit=500&order=desc&archived=false"


def test_session_detail_returns_data_object(monkeypatch):
    calls = []
    patch_urlopen(monkeypatch, calls, {"data": {"id": "ses_1", "title": "T"}})
    client = RestClient(info())
    assert client.session_detail("ses_1") == {"id": "ses_1", "title": "T"}
    assert calls[0][0] == BASE + "/api/session/ses_1"


def test_pending_endpoints_unwrap_data_list(monkeypatch):
    calls = []
    patch_urlopen(monkeypatch, calls, {"data": [{"id": "prq_1", "sessionID": "ses_1"}]})
    client = RestClient(info())
    assert client.pending_permissions() == [{"id": "prq_1", "sessionID": "ses_1"}]
    assert calls[0][0] == BASE + "/api/permission/request"
    patch_urlopen(monkeypatch, calls, {"no_data": True})
    assert client.pending_forms() == []
    assert calls[1][0] == BASE + "/api/form"


def test_fetch_info_hits_api_info(monkeypatch):
    calls = []
    patch_urlopen(monkeypatch, calls, {"version": "2.1.3", "pid": 123})
    svc = ServiceInfo(id="test-id", version="2.1.3", url=BASE + "/", pid=123, password=PW)
    assert fetch_info(svc, timeout=2.5)["version"] == "2.1.3"
    url, auth, timeout = calls[0]
    assert url == BASE + "/api/info"
    assert auth == basic_auth_header(PW)
    assert timeout == 2.5



def test_snapshot_carries_outcome_for_patch_semantics():
    client = FakeClient(
        pages=[page([sess("ses_ok", outcome="succeeded"),
                     sess("ses_i", outcome="interrupted"),
                     sess("ses_bare")], None)],
    )
    got = by_id(build_snapshot(client))
    assert got["ses_ok"].outcome == "succeeded"
    assert got["ses_i"].outcome == "interrupted"
    assert got["ses_bare"].outcome is None
