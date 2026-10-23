"""解析层测试：原始 SSE 帧 dict → Event 字段映射（补齐此前无红灯保护的解析缝隙）。"""
from traffic_light.core import parse_event


def test_permission_asked_takes_id():
    ev = parse_event({"type": "permission.asked", "data": {"id": "prq_1", "sessionID": "ses_a"}})
    assert ev is not None
    assert ev.request_id == "prq_1"
    assert ev.session_id == "ses_a"


def test_permission_replied_takes_request_id():
    ev = parse_event({"type": "permission.replied",
                      "data": {"sessionID": "ses_a", "requestID": "prq_1", "reply": "once"}})
    assert ev.request_id == "prq_1"


def test_form_created_takes_form_id_and_session_fallback():
    ev = parse_event({"type": "form.created", "data": {"form": {"id": "frm_1", "sessionID": "ses_b"}}})
    assert ev.request_id == "frm_1"
    assert ev.session_id == "ses_b"


def test_form_cancelled_takes_id():
    ev = parse_event({"type": "form.cancelled", "data": {"id": "frm_9", "sessionID": "ses_b"}})
    assert ev.request_id == "frm_9"


def test_session_created_takes_parent_and_optional_title():
    ev = parse_event({"type": "session.created",
                      "data": {"sessionID": "ses_c", "parentID": "ses_p", "title": "T"}})
    assert ev.parent_id == "ses_p"
    assert ev.title == "T"


def test_interrupted_takes_reason():
    ev = parse_event({"type": "session.execution.interrupted",
                      "data": {"sessionID": "ses_a", "reason": "user"}})
    assert ev.reason == "user"


def test_directory_from_envelope_location_wins():
    ev = parse_event({"type": "session.renamed",
                      "location": {"directory": "D:\\dev\\x"},
                      "data": {"sessionID": "ses_a", "title": "T"}})
    assert ev.directory == "D:\\dev\\x"
    assert ev.title == "T"


def test_directory_falls_back_to_data_location():
    ev = parse_event({"type": "session.created",
                      "data": {"sessionID": "ses_c", "location": {"directory": "D:\\dev\\y"}}})
    assert ev.directory == "D:\\dev\\y"


def test_bad_inputs_return_none():
    assert parse_event({}) is None
    assert parse_event({"type": 123}) is None
    assert parse_event("nope") is None