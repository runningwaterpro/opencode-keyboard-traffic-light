"""SSE 解析测试：真实线格式、心跳、行尾、坏帧与截断半帧。"""
from traffic_light.sse import SSEFrameParser, parse_sse_lines

# 实机抓包样例帧
FRAME_1 = 'data: {"id":"evt_1","created":1792747599399,"type":"session.reasoning.delta","data":{"sessionID":"ses_1","delta":"..."}}'
FRAME_2 = 'data: {"id":"evt_2","created":1792747600651,"type":"session.execution.started","data":{"sessionID":"ses_1"},"durable":{"aggregateID":"ses_1","seq":2,"version":1}}'


def collect(lines):
    """喂入行序列，收集产出的帧。"""
    return list(parse_sse_lines(lines))


def test_single_frame():
    frames = collect([FRAME_1, ""])
    assert len(frames) == 1
    assert frames[0]["id"] == "evt_1"
    assert frames[0]["type"] == "session.reasoning.delta"
    assert frames[0]["data"]["sessionID"] == "ses_1"


def test_multiple_frames_with_heartbeats_between():
    frames = collect([FRAME_1, "", ": heartbeat", "", FRAME_2, ""])
    assert [f["id"] for f in frames] == ["evt_1", "evt_2"]
    assert frames[1]["durable"] == {"aggregateID": "ses_1", "seq": 2, "version": 1}


def test_crlf_line_endings():
    frames = collect(['data: {"x": 1}\r\n', "\r\n", ": heartbeat\r\n", 'data: {"x": 2}\r\n', "\r\n"])
    assert frames == [{"x": 1}, {"x": 2}]


def test_multiline_data_joined_with_newline():
    frames = collect(['data: {"x":', "data: 1}", ""])
    assert frames == [{"x": 1}]


def test_multiline_data_with_token_on_each_line():
    frames = collect(['data: {"type":', 'data: "session.created"}', ""])
    assert frames == [{"type": "session.created"}]


def test_heartbeat_between_data_lines_ignored():
    frames = collect(['data: {"x":', ": heartbeat", "data: 1}", ""])
    assert frames == [{"x": 1}]


def test_bad_json_frame_skipped_and_later_frames_survive():
    frames = collect(["data: {oops", "", FRAME_1, ""])
    assert [f["id"] for f in frames] == ["evt_1"]


def test_non_dict_json_skipped():
    frames = collect(['data: [1, 2]', "", 'data: "hello"', "", "data: 42", "", "data: null", "", FRAME_2, ""])
    assert [f["id"] for f in frames] == ["evt_2"]


def test_truncated_half_frame_not_yielded():
    # 流末尾是被截断的半帧：JSON 不完整，无空行收尾
    frames = collect([FRAME_1, "", 'data: {"id":"evt_3","type":"session.exec'])
    assert [f["id"] for f in frames] == ["evt_1"]


def test_truncated_half_frame_at_stream_end_is_safe():
    assert collect(['data: {"a":']) == []


def test_valid_json_without_trailing_blank_line_is_flushed_at_end():
    assert collect(['data: {"x": 1}']) == [{"x": 1}]


def test_data_without_space_after_colon():
    assert collect(['data:{"x":1}', ""]) == [{"x": 1}]


def test_only_one_space_after_colon_is_eaten():
    assert collect(['data: {"s": "  padded"}', ""]) == [{"s": "  padded"}]


def test_other_field_lines_ignored():
    frames = collect(["id: 7", "event: session.created", "retry: 1000", 'data: {"x": 1}', ""])
    assert frames == [{"x": 1}]


def test_empty_input_yields_no_frames():
    assert collect([]) == []


def test_pure_heartbeats_yield_no_frames():
    assert collect([": heartbeat", "", ": heartbeat", ""]) == []


def test_frame_boundary_resets_buffer():
    frames = collect(['data: {"x": 1}', "", "", FRAME_1, ""])
    assert len(frames) == 2
    assert frames[0] == {"x": 1}
    assert frames[1]["id"] == "evt_1"


def test_feed_returns_frame_only_at_boundary():
    p = SSEFrameParser()
    assert p.feed('data: {"x": 1}') is None
    assert p.feed(": heartbeat") is None
    assert p.feed("") == {"x": 1}
    assert p.feed("") is None  # 连续空行不再产出


def test_feed_bad_json_then_reuse_parser():
    p = SSEFrameParser()
    assert p.feed("data: broken") is None
    assert p.feed("") is None  # 坏 JSON 丢弃
    assert p.feed('data: {"ok": true}') is None
    assert p.feed("") == {"ok": True}