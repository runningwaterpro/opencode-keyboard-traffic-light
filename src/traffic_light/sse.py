"""SSE 帧解析：把 OpenCode V2 /api/event 的行流解析成事件 dict。

纯解析层，不涉及网络。真实线格式（实机抓包确认）：

- 数据帧是单行 ``data: {json}``；没有 ``event:`` 字段，事件类型在 JSON 的 "type" 里；
- 心跳/注释行以 ``:`` 开头，如 ``: heartbeat``；
- 帧与帧之间用空行分隔；行尾可能是 CRLF 或 LF；
- 流尾部可能截断成半帧，必须安全丢弃，不抛异常。
"""

import json
from collections.abc import Iterable, Iterator

__all__ = ["SSEFrameParser", "parse_sse_lines"]


class SSEFrameParser:
    """行驱动的增量解析器：逐行喂入，凑齐一帧时返回 dict。"""

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> dict | None:
        """喂入一行原始行（可含结尾 CRLF 或 LF）。一帧完成时返回解析出的 dict，否则 None。"""
        line = line.rstrip("\r\n")
        if line == "":
            return self._flush()  # 空行 = 帧边界
        if line.startswith(":"):
            return None  # 心跳/注释，忽略
        if line.startswith("data:"):
            payload = line[len("data:"):]
            if payload.startswith(" "):
                payload = payload[1:]  # 规范：冒号后的一个空格吃掉
            self._data.append(payload)
        # 其他字段行（id:/event:/retry:）一律忽略
        return None

    def _flush(self) -> dict | None:
        """帧边界：把缓冲的 data 行用换行拼成 JSON；坏 JSON 或非 dict 丢弃。"""
        if not self._data:
            return None
        raw = "\n".join(self._data)
        self._data = []
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


def parse_sse_lines(lines: Iterable[str]) -> Iterator[dict]:
    """逐行喂入并产出完整帧；结束时补出缓冲中的合法 JSON，半帧/坏 JSON 安全丢弃。"""
    parser = SSEFrameParser()
    for line in lines:
        frame = parser.feed(line)
        if frame is not None:
            yield frame
    tail = parser.feed("")  # 输入结束也视为一次帧边界
    if tail is not None:
        yield tail