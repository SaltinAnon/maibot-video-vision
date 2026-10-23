"""视频消息识别：从宿主消息字典里找出「这里有视频」的证据。

为什么需要这一层：
统一 QQ 连接器（MaiBot-SnowLuma-Adapter）在 `codecs/inbound/message_codec.py`
里把 OneBot 的 `video` 段直接降级成一段文本：

    [视频] 文件: 1A2B3C.mp4，大小: 2048576

也就是说**视频本体（url / file）根本不会传到 MaiBot**。所以插件要做两件事：
1. 用这段占位文本判断「这条消息里有视频」（本模块）；
2. 拿着 message_id 回头调用适配器的 `get_msg` 动作，把原始段取回来（source.py）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

# 适配器降级文本的兜底正则：文件 + 大小
_HINT_FILE_RE = re.compile(r"文件[:：]\s*(?P<file>[^，,；;\n]+)")
_HINT_SIZE_RE = re.compile(r"大小[:：]\s*(?P<size>\d+)")
# CQ 码中的 video 段，例如 [CQ:video,file=http://x/1.mp4]
_CQ_RE = re.compile(r"\[CQ:video,(?P<params>[^\]]*)\]", re.IGNORECASE)

_VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".ts")


def compile_markers(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """把配置里的字符串正则编译成 Pattern 列表，非法正则直接跳过。

    Args:
        patterns: 配置中的正则字符串列表。

    Returns:
        tuple[re.Pattern, ...]: 编译成功的正则元组。
    """

    compiled: list[re.Pattern[str]] = []
    for raw in patterns or ():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            compiled.append(re.compile(text, re.IGNORECASE))
        except re.error:
            continue
    return tuple(compiled)


def iter_segments(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取出消息的 raw_message 段列表（过滤掉非法项）。

    Args:
        message: 宿主传给钩子的消息字典。

    Returns:
        list[dict]: 消息段列表；结构不合法时返回空列表。
    """

    raw = message.get("raw_message")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def segment_text(segment: Mapping[str, Any]) -> str:
    """读取文本段的文本内容。

    Args:
        segment: 单个消息段。

    Returns:
        str: 文本内容；非文本段返回空字符串。
    """

    if str(segment.get("type") or "").strip().lower() != "text":
        return ""
    data = segment.get("data")
    return data if isinstance(data, str) else ""


def is_marker_segment(segment: Mapping[str, Any], markers: Sequence[re.Pattern[str]]) -> bool:
    """判断单个消息段是否代表「这里有视频」。

    两种命中方式：
    - 段类型本身就是 video（某些适配器会保留原始段，属于未来兼容）；
    - 文本段命中配置的正则（统一 QQ 连接器的降级文本走这条）。

    Args:
        segment: 单个消息段。
        markers: 已编译的正则列表。

    Returns:
        bool: 是否命中。
    """

    segment_type = str(segment.get("type") or "").strip().lower()
    if segment_type == "video":
        return True

    text = segment_text(segment)
    if not text:
        return False
    return any(pattern.search(text) for pattern in markers)


def find_marker_indices(
    message: Mapping[str, Any],
    markers: Sequence[re.Pattern[str]],
) -> list[int]:
    """返回所有视频标记段的下标。

    Args:
        message: 宿主消息字典。
        markers: 已编译的正则列表。

    Returns:
        list[int]: 命中段的下标列表，按原顺序。
    """

    return [index for index, segment in enumerate(iter_segments(message)) if is_marker_segment(segment, markers)]


def parse_marker_hint(text: str) -> dict[str, Any]:
    """从适配器降级文本里抠出文件名和大小，用于零成本的缓存键。

    Args:
        text: 例如 ``[视频] 文件: 1A2B.mp4，大小: 2048576``。

    Returns:
        dict: 可能包含 ``file`` 与 ``size`` 两个键。
    """

    hint: dict[str, Any] = {}
    if not text:
        return hint
    file_match = _HINT_FILE_RE.search(text)
    if file_match:
        file_name = file_match.group("file").strip()
        if file_name:
            hint["file"] = file_name
    size_match = _HINT_SIZE_RE.search(text)
    if size_match:
        try:
            hint["size"] = int(size_match.group("size"))
        except (TypeError, ValueError):
            pass
    return hint


def is_private_chat(message: Mapping[str, Any]) -> bool:
    """判断是否私聊。

    Args:
        message: 宿主消息字典。

    Returns:
        bool: 私聊为 True。
    """

    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return False
    group_info = message_info.get("group_info")
    return not isinstance(group_info, Mapping)


def is_addressed(message: Mapping[str, Any]) -> bool:
    """判断这条消息是否 @ 了机器人（或被判定为对机器人说话）。

    Args:
        message: 宿主消息字典。

    Returns:
        bool: 命中 @ / mention 时为 True。
    """

    if message.get("is_at") or message.get("is_mentioned"):
        return True
    for segment in iter_segments(message):
        if str(segment.get("type") or "").strip().lower() != "at":
            continue
        data = segment.get("data")
        if isinstance(data, Mapping) and data.get("target_user_id"):
            return True
    return False


def _parse_cq_params(params: str) -> dict[str, str]:
    """解析 CQ 码参数串。

    Args:
        params: 逗号分隔的 ``key=value`` 串。

    Returns:
        dict: 参数映射（已做 CQ 反转义）。
    """

    result: dict[str, str] = {}
    for chunk in params.split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        normalized = value.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")
        result[key.strip().lower()] = unquote(normalized).strip()
    return result


def find_video_payload(payload: Any, _depth: int = 0) -> dict[str, Any] | None:
    """在任意嵌套结构里递归寻找 OneBot 的 video 段数据。

    `get_msg` 的返回在不同协议端上包了好几层（``data.message``、``message``、
    合并转发里的 ``data[].content``），所以这里做一次深度优先遍历，兼容
    list / dict / CQ 码字符串三种形态。

    Args:
        payload: 待搜索的对象，通常是 ``get_msg`` 的返回值。
        _depth: 内部递归深度保护。

    Returns:
        dict | None: 找到的 video 段 ``data`` 字典；未找到返回 None。
    """

    if _depth > 8 or payload is None:
        return None

    if isinstance(payload, str):
        match = _CQ_RE.search(payload)
        if match:
            return _parse_cq_params(match.group("params"))
        return None

    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = find_video_payload(item, _depth + 1)
            if found is not None:
                return found
        return None

    if not isinstance(payload, Mapping):
        return None

    segment_type = str(payload.get("type") or "").strip().lower()
    segment_data = payload.get("data")
    if segment_type == "video" and isinstance(segment_data, Mapping):
        return dict(segment_data)
    if segment_type == "video" and isinstance(segment_data, str):
        return {"file": segment_data}

    for value in payload.values():
        found = find_video_payload(value, _depth + 1)
        if found is not None:
            return found
    return None


def looks_like_video_file(name: str) -> bool:
    """按扩展名粗判一个文件名是否像视频。

    Args:
        name: 文件名或 URL。

    Returns:
        bool: 像视频时为 True。
    """

    lowered = (name or "").split("?")[0].lower()
    return lowered.endswith(_VIDEO_EXTENSIONS)


def guess_extension(payload: Mapping[str, Any]) -> str:
    """从段数据里猜一个可用的容器扩展名，供 ffmpeg 判断 demuxer。

    Args:
        payload: video 段数据。

    Returns:
        str: 带点的扩展名，默认 ``.mp4``。
    """

    for key in ("file", "filename", "name"):
        value = payload.get(key)
        if isinstance(value, str) and looks_like_video_file(value):
            return "." + value.split("?")[0].rsplit(".", 1)[-1].lower()
    return ".mp4"
