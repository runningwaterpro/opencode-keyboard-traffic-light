# -*- coding: utf-8 -*-
"""机型档案测试：纯离线，不碰真设备、不枚举。"""
import pytest

from traffic_light import profiles
from traffic_light.device import HidDeviceInfo


def info(vid, pid, usage_page=0xFF00, usage=1, feature=520, path=None):
    path = path or (r"\\?\hid#vid_%04x&pid_%04x&col06#1" % (vid, pid))
    return HidDeviceInfo(path=path, vid=vid, pid=pid, usage_page=usage_page,
                         usage=usage, feature_length=feature)


def test_hi86_capability_verified_and_full_keys():
    cap = profiles.Hi86Profile().capability
    assert cap.verified is True
    assert cap.keys == ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")
    assert cap.rgb and cap.breathing


def test_m75v2_declares_capability_but_unverified():
    cap = profiles.M75V2Profile().capability
    assert cap.verified is False
    assert cap.note
    assert cap.keys == profiles.NUMBER_KEYS


def test_m75v2_refuses_all_write_paths():
    p = profiles.M75V2Profile()
    d = info(0x0C45, 0x8006)
    ok, why = p.validate(d)
    assert ok is False and why
    for call in (lambda: p.open(d), lambda: p.render({}, 0), lambda: p.write(1, b"x")):
        with pytest.raises(RuntimeError):
            call()


def test_m75v2_detect_modes():
    p = profiles.M75V2Profile()
    assert p.detect_mode([info(0x0C45, 0x8006)]) == profiles.MODE_WIRED
    assert p.detect_mode([info(0x0C45, 0x8007)]) == profiles.MODE_WIRED
    assert p.detect_mode([info(0x05AC, 0x024F)]) == profiles.MODE_24G
    assert p.detect_mode([info(0x1234, 0x5678)]) == profiles.MODE_NONE


def test_m75v2_find_channel_returns_matching_info():
    p = profiles.M75V2Profile()
    target = info(0x0C45, 0x8007)
    assert p.find_channel([info(0x1111, 0x2222), target]) is target


def test_hi86_detect_uses_device_logic():
    p = profiles.Hi86Profile()
    assert p.detect_mode([info(0x258A, 0x010C)]) == profiles.MODE_WIRED
    assert p.detect_mode([info(0x3554, 0xFA09, usage_page=0xFF04, usage=2,
                              feature=8)]) == profiles.MODE_24G
    assert p.detect_mode([]) == profiles.MODE_NONE


def test_select_profile_never_returns_unverified():
    assert profiles.select_profile([info(0x0C45, 0x8006)]) is None
    picked = profiles.select_profile([info(0x258A, 0x010C)])
    assert picked is not None and picked.capability.name == "hi86"


def test_probe_rows_filters_candidates_and_all():
    hi86 = info(0x258A, 0x010C)
    m75 = info(0x0C45, 0x8006)
    other = info(0x1111, 0x2222, usage_page=0x0001)
    devs = [hi86, m75, other]
    got = {(d.vid, d.pid) for d in profiles.probe_rows(devs)}
    assert got == {(0x258A, 0x010C), (0x0C45, 0x8006)}
    assert profiles.probe_rows(devs, include_all=True) == [hi86, m75]


def test_get_profile_rejects_unknown_name():
    assert profiles.get_profile("hi86").capability.name == "hi86"
    with pytest.raises(ValueError):
        profiles.get_profile("nope")

def test_hi86_list_channels_only_valid():
    p = profiles.Hi86Profile()
    good = info(0x258A, 0x010C)
    bad = info(0x258A, 0x010C, feature=6)
    assert p.list_channels([bad, good]) == [good]


def test_m75v2_list_channels_empty():
    assert profiles.M75V2Profile().list_channels([info(0x0C45, 0x8006)]) == []


def test_verified_profiles_excludes_unverified():
    names = [p.capability.name for p in profiles.verified_profiles()]
    assert "hi86" in names
    assert "m75v2" not in names