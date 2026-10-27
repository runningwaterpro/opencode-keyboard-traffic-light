"""notify 模块离线测试：文案矩阵、短 id、干跑日志、缺库降级。

绝不弹真实通知：winotify 未装时走真实降级路径；若本机装了，用假模块或 None 堵住。
"""
import importlib
import logging
import sys
import types

import pytest

from traffic_light.notify import (
    DryRunNotifier,
    NotifyRequest,
    WinotifyNotifier,
    create_notifier,
    format_notify,
)

LOG_NAME = "traffic_light.notify"
KINDS = (
    ("permission", "需授权"),
    ("question", "提问"),
    ("completed", "完成"),
    ("failed", "失败"),
    ("interrupted", "中断"),
)


def hide_winotify(monkeypatch) -> None:
    """保证本次测试里 winotify 不可导入（装了就用 None 堵住 sys.modules）。"""
    try:
        importlib.import_module("winotify")
    except ImportError:
        return
    monkeypatch.setitem(sys.modules, "winotify", None)


@pytest.mark.parametrize(("kind", "prefix"), KINDS)
def test_title_with_slot(kind, prefix):
    head, _ = format_notify(NotifyRequest(kind=kind, slot="3"))
    assert head == f"{prefix} · #3"


@pytest.mark.parametrize(("kind", "prefix"), KINDS)
def test_title_without_slot(kind, prefix):
    head, _ = format_notify(NotifyRequest(kind=kind, slot=None))
    assert head == prefix


def test_slot_zero_counts_as_number():
    head, _ = format_notify(NotifyRequest(kind="permission", slot="0"))
    assert head == "需授权 · #0"


@pytest.mark.parametrize("slot", [None, "3"])
def test_cable_ignores_slot(slot):
    head, _ = format_notify(NotifyRequest(kind="cable", slot=slot))
    assert head == "键盘需要有线连接"


def test_wireless_tip_has_fixed_title_and_body():
    """一次性说明：标题就说事，正文写清「打字时不发帧」和怎么关。"""
    head, body = format_notify(NotifyRequest(kind="wireless_tip"))
    assert head == "无线键盘也会亮灯"
    assert "打字时不发帧" in body
    assert "托盘菜单" in body


def test_body_project_and_title():
    _, body = format_notify(NotifyRequest(kind="completed", project="demo", title="会话一"))
    assert body == "demo — 会话一"


def test_body_only_project():
    _, body = format_notify(NotifyRequest(kind="completed", project="demo"))
    assert body == "demo"


def test_body_only_title():
    _, body = format_notify(NotifyRequest(kind="completed", title="会话一"))
    assert body == "会话一"


def test_body_short_session_id():
    _, body = format_notify(NotifyRequest(kind="completed", session_id="ses_e9868cfd8ffe"))
    assert body == "ses_e9868cfd"
    assert len(body) == 12


def test_body_short_session_id_below_limit_kept():
    _, body = format_notify(NotifyRequest(kind="completed", session_id="ses_1"))
    assert body == "ses_1"


def test_body_empty_when_all_absent():
    _, body = format_notify(NotifyRequest(kind="completed"))
    assert body == ""

def test_permission_detail_appended():
    _, body = format_notify(
        NotifyRequest(kind="permission", project="demo", title="会话一", detail=r"C:\repo\a.txt")
    )
    assert body == "demo — 会话一\nC:\\repo\\a.txt"


def test_detail_not_appended_for_non_permission():
    _, body = format_notify(
        NotifyRequest(kind="question", project="demo", detail=r"C:\repo\a.txt")
    )
    assert body == "demo"


def test_dry_run_send_logs(caplog):
    log = logging.getLogger(LOG_NAME)
    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        DryRunNotifier(log).send(
            NotifyRequest(kind="completed", slot="3", project="demo", title="会话一")
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("[通知]") for m in messages)
    joined = "\n".join(messages)
    assert "完成 · #3" in joined
    assert "demo — 会话一" in joined
    assert "｜" in joined


def test_winotify_missing_warns_once_then_logs(monkeypatch, caplog):
    hide_winotify(monkeypatch)
    log = logging.getLogger(LOG_NAME)
    notifier = WinotifyNotifier(log)
    req = NotifyRequest(kind="failed", slot="2", project="demo", title="会话一")
    with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
        notifier.send(req)  # 首次：缺库 → warning + 降级日志
        notifier.send(req)  # 之后：本实例直接退回日志，不再 warning
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["未安装 winotify，通知降级为日志"]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert sum(m.startswith("[通知]") for m in infos) == 2


def test_winotify_success_path_with_fake_module(monkeypatch):
    calls: dict[str, object] = {}

    class FakeNotification:
        def __init__(self, app_id, title, msg, duration):
            calls.update(app_id=app_id, title=title, msg=msg, duration=duration)

        def set_audio(self, sound):
            calls["sound"] = sound

        def show(self):
            calls["shown"] = True

    fake = types.ModuleType("winotify")
    fake.audio = types.SimpleNamespace(Default="默认提示音")
    fake.Notification = FakeNotification
    monkeypatch.setitem(sys.modules, "winotify", fake)

    WinotifyNotifier(logging.getLogger(LOG_NAME)).send(
        NotifyRequest(kind="question", slot="1", project="demo", title="会话一")
    )
    assert calls == {
        "app_id": "OpenCode 状态灯",
        "title": "提问 · #1",
        "msg": "demo — 会话一",
        "duration": "short",
        "sound": "默认提示音",
        "shown": True,
    }


def test_winotify_show_error_swallowed_to_debug(monkeypatch, caplog):
    class FakeNotification:
        def __init__(self, **kwargs):
            pass

        def set_audio(self, sound):
            pass

        def show(self):
            raise RuntimeError("模拟发送失败")

    fake = types.ModuleType("winotify")
    fake.audio = types.SimpleNamespace(Default=None)
    fake.Notification = FakeNotification
    monkeypatch.setitem(sys.modules, "winotify", fake)

    with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
        WinotifyNotifier(logging.getLogger(LOG_NAME)).send(NotifyRequest(kind="completed"))
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_create_notifier_types():
    log = logging.getLogger(LOG_NAME)
    assert isinstance(create_notifier(dry_run=True, log=log), DryRunNotifier)
    assert isinstance(create_notifier(dry_run=False, log=log), WinotifyNotifier)